"""Compiled `.dante` bytecode back to readable `.dn` source.

The inverse of dante.compiler: it rebuilds expressions, control flow and locals."""
import os
import re
import sys
import collections
import tempfile

from . import module as DV
from . import compiler as DC

s16 = DV.s16
s32 = DV.s32
bitsf = DV.bitsf
NAMEOP = DV.NAMEOP
OPS = DV.OPS


class Bail(Exception):
    """Raised when a function cannot be structured; the caller falls back."""


# --------------------------------------------------------------------------
# expressions
P_OR, P_AND, P_EQ, P_REL, P_ADD, P_MUL, P_UN, P_ATOM = 0, 1, 2, 3, 4, 5, 6, 7


class Ex:
    __slots__ = ("s", "p", "t", "lea")

    def __init__(self, s, p=P_ATOM, t=None, lea=False):
        self.s = s
        self.p = p
        self.t = t
        self.lea = lea          # value came from LEA (an address-of / object)

    def txt(self, minp=0):
        return self.s if self.p >= minp else "(%s)" % self.s

    def __repr__(self):
        return "Ex(%s)" % self.s


def binop(a, op, b, p, t=None):
    return Ex("%s %s %s" % (a.txt(p), op, b.txt(p + 1)), p, t)


def fmt_float(v):
    """Shortest decimal that re-parses to the same 32-bit float."""
    f = bitsf(v)
    if f != f or f in (float("inf"), float("-inf")):
        return "%d" % v          # not representable in source; keep the bits
    for prec in range(1, 18):
        r = "%.*g" % (prec, f)
        try:
            if DV.fbits(float(r)) != (v & 0xFFFFFFFF):
                continue
        except ValueError:
            continue
        if "e" in r or "E" in r:
            try:
                import decimal
                d = format(decimal.Decimal(r), "f")
                if float(d) == float(r) and len(d) <= 24:
                    r = d
            except Exception:
                pass
        if "." not in r and "e" not in r and "E" not in r:
            r += ".0"
        return r
    return "%r" % f


# --------------------------------------------------------------------------
# statements
class St:
    __slots__ = ("kind", "a", "b", "c", "d", "decl", "slot", "size", "iend")

    def __init__(self, kind, a=None, b=None, c=None, d=None, decl=None):
        self.kind = kind
        self.a, self.b, self.c, self.d = a, b, c, d
        self.decl = decl        # ("<type>", "<name>", "<rhs or None>") for declarations
        self.slot = None        # the stack slot that declaration occupies
        self.size = 4
        self.iend = None        # loops: first instruction index after the loop


def decl_st(tname, name, rhs=None, slot=None, size=4):
    # `.dn` cannot declare a local of function type; `int` has the same layout
    note = ""
    if tname.startswith("("):
        note = "   // %s" % tname
        tname = "int"
    st = St("raw", ("%s %s = %s;%s" % (tname, name, rhs, note)) if rhs is not None
            else ("%s %s;%s" % (tname, name, note)))
    st.decl = (tname, name, rhs)
    st.slot = slot
    st.size = max(4, size)
    return st


def _st_own_text(st):
    """The text of a statement's own line/header (not of any nested body)."""
    if st.kind in ("comment", "drop"):
        return ""
    if st.kind == "for":
        return "%s;%s;%s" % (st.a, st.b, st.c)
    if st.kind == "try":
        return ""
    return str(st.a)


_STRLIT = re.compile(r'"(?:\\.|[^"\\])*"')


def strip_strings(t):
    return _STRLIT.sub('""', t)


def subst_ident(t, old, new):
    """Rename identifier `old` outside string literals."""
    rx = re.compile(r"\b%s\b" % re.escape(old))
    out = []
    pos = 0
    for m in _STRLIT.finditer(t):
        out.append(rx.sub(new, t[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(rx.sub(new, t[pos:]))
    return "".join(out)


def walk_stmts(lst):
    for st in lst:
        yield st
        if st.kind == "if":
            for x in walk_stmts(st.b):
                yield x
            if st.c:
                for x in walk_stmts(st.c):
                    yield x
        elif st.kind in ("while", "do"):
            for x in walk_stmts(st.b):
                yield x
        elif st.kind == "try":
            for x in walk_stmts(st.b):
                yield x
            for x in walk_stmts(st.c):
                yield x
        elif st.kind == "for":
            for x in walk_stmts(st.d):
                yield x


def rename_stmt(st, old, new):
    if st.kind == "for":
        st.a, st.b, st.c = (subst_ident(x, old, new) for x in (st.a, st.b, st.c))
    elif st.kind in ("raw", "comment"):
        st.a = subst_ident(str(st.a), old, new)
    elif st.kind in ("if", "while", "do"):
        st.a = subst_ident(str(st.a), old, new)
    if st.decl is not None and st.decl[1] == old:
        st.decl = (st.decl[0], new, st.decl[2])


def render(stmts, ind=1, out=None):
    out = [] if out is None else out
    pad = "    " * ind
    for s in stmts:
        k = s.kind
        if k == "drop":
            continue
        if k == "raw":
            out.append(pad + s.a)
        elif k == "comment":
            for ln in s.a.split("\n"):
                out.append(pad + "// " + ln)
        elif k == "if":
            out.append(pad + "if (%s) {" % s.a)
            render(s.b, ind + 1, out)
            if s.c:
                if len(s.c) == 1 and s.c[0].kind == "if":
                    tail = []
                    render(s.c, ind, tail)
                    tail[0] = tail[0].lstrip()
                    out.append(pad + "} else " + tail[0])
                    out.extend(tail[1:])
                    continue
                out.append(pad + "} else {")
                render(s.c, ind + 1, out)
            out.append(pad + "}")
        elif k == "while":
            out.append(pad + "while (%s) {" % s.a)
            render(s.b, ind + 1, out)
            out.append(pad + "}")
        elif k == "do":
            out.append(pad + "do {")
            render(s.b, ind + 1, out)
            out.append(pad + "} while (%s);" % s.a)
        elif k == "try":
            out.append(pad + "try {")
            render(s.b, ind + 1, out)
            out.append(pad + "} catch {")
            render(s.c, ind + 1, out)
            out.append(pad + "}")
        elif k == "for":
            out.append(pad + "for (%s; %s; %s) {" % (s.a, s.b, s.c))
            render(s.d, ind + 1, out)
            out.append(pad + "}")
        elif k == "label":
            out.append("%s:" % s.a)
        elif k == "_try":
            out.append(pad + "// TRY -> %s   (exception scope not folded back)" % s.a)
        elif k == "_endtry":
            out.append(pad + "// ENDTRY   (exception scope not folded back)")
        else:
            out.append(pad + str(s.a))
    return out


def falls(stmts):
    """False when the statement list cannot fall out of its end."""
    if not stmts:
        return True
    last = stmts[-1]
    if last.kind == "raw" and last.a.split("(")[0].strip() in ("return;", "return", "break;", "continue;"):
        return False
    if last.kind == "raw" and (last.a.startswith("return ") or last.a == "return;"
                               or last.a == "break;" or last.a == "continue;"):
        return False
    if last.kind == "if" and last.c:
        return falls(last.b) or falls(last.c)
    return True


# --------------------------------------------------------------------------
def is_jump(ins):
    return OPS[ins.op][1] == "j"


def is_goto(ins):
    return (ins.op == NAMEOP["JEQ_I"] and ins.ops[1].mode == 0 and ins.ops[2].mode == 0)


CMP_FALSE = {"JLT": ">=", "JLE": ">", "JEQ": "!=", "JNE": "=="}
CMP_TRUE = {"JLT": "<", "JLE": "<=", "JEQ": "==", "JNE": "!="}

ARITH_OP = {
    "ADDI": ("+", P_ADD, "int"), "SUBI": ("-", P_ADD, "int"),
    "MULI": ("*", P_MUL, "int"), "DIVI": ("/", P_MUL, "int"), "MODI": ("%", P_MUL, "int"),
    "ADDF": ("+", P_ADD, "float"), "SUBF": ("-", P_ADD, "float"),
    "MULF": ("*", P_MUL, "float"), "DIVF": ("/", P_MUL, "float"), "MODF": ("%", P_MUL, "float"),
    "ADDV": ("+", P_ADD, "Vector"), "SUBV": ("-", P_ADD, "Vector"),
    "MULVS": ("*", P_MUL, "Vector"), "DIVVS": ("/", P_MUL, "Vector"),
    "STRCAT": ("+", P_ADD, "String"),
}
SET_OP = {"LT_I": ("<", "int"), "LT_F": ("<", "float"), "LE_I": ("<=", "int"), "LE_F": ("<=", "float"),
          "EQ_I": ("==", "int"), "EQ_F": ("==", "float"), "EQ_V": ("==", "Vector"),
          "EQ_S": ("==", "String"), "EQ_B": ("==", "bool"),
          "NE_I": ("!=", "int"), "NE_F": ("!=", "float"), "NE_V": ("!=", "Vector"),
          "NE_S": ("!=", "String"), "NE_B": ("!=", "bool")}
DEST_TYPE = {"ADDI": "int", "SUBI": "int", "MULI": "int", "DIVI": "int", "MODI": "int",
             "ADDF": "float", "SUBF": "float", "MULF": "float", "DIVF": "float", "MODF": "float",
             "ADDV": "Vector", "SUBV": "Vector", "MULVS": "Vector", "DIVVS": "Vector",
             "STRCAT": "String", "STRCPY": "String", "STRNEW": "String",
             "MOVV": "Vector", "DOT": "float", "FTOI": "int", "ITOF": "float",
             "ITOS": "String", "FTOS": "String", "BTOS": "String", "VTOS": "String",
             "LT_I": "bool", "LT_F": "bool", "LE_I": "bool", "LE_F": "bool",
             "EQ_I": "bool", "EQ_F": "bool", "EQ_V": "bool", "EQ_S": "bool", "EQ_B": "bool",
             "NE_I": "bool", "NE_F": "bool", "NE_V": "bool", "NE_S": "bool", "NE_B": "bool"}
SRC_TYPE = {"ADDI": "int", "SUBI": "int", "MULI": "int", "DIVI": "int", "MODI": "int",
            "ADDF": "float", "SUBF": "float", "MULF": "float", "DIVF": "float", "MODF": "float",
            "ADDV": "Vector", "SUBV": "Vector", "STRCAT": "String", "STRCPY": "String",
            "MOVV": "Vector", "DOT": "Vector", "FTOI": "float", "ITOF": "int",
            "ITOS": "int", "FTOS": "float", "BTOS": "bool", "VTOS": "Vector",
            "LT_I": "int", "LT_F": "float", "LE_I": "int", "LE_F": "float",
            "EQ_F": "float", "EQ_V": "Vector", "EQ_S": "String", "EQ_B": "bool",
            "NE_F": "float", "NE_V": "Vector", "NE_S": "String", "NE_B": "bool"}
JCMP_TYPE = {"JLT_I": "int", "JLE_I": "int", "JLT_F": "float", "JLE_F": "float",
             "JEQ_F": "float", "JNE_F": "float", "JEQ_V": "Vector", "JNE_V": "Vector",
             "JEQ_S": "String", "JNE_S": "String", "JEQ_B": "bool", "JNE_B": "bool"}

KEYWORDS = DC.KEYWORDS


def ident(name):
    name = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not name or name[0].isdigit():
        name = "_" + name
    if name in KEYWORDS:
        name += "_"
    return name


# ==========================================================================
class Module:
    """One decompiled .dante module."""

    def __init__(self, path, extra_symbols=()):
        self.path = path
        self.d = DV.load(path)
        stem = self.d.stem_data or ""
        self.stem = stem[2:-5] if stem.startswith("__") else os.path.splitext(os.path.basename(path))[0]
        self.types = DC.Types()
        self.db = DC.SymbolDB(self.types)
        for kind, p in DC.default_symbol_paths():
            (self.db.load_api if kind == "api" else self.db.load_json)(p)
        for p in extra_symbols:
            self.db.load_json(p)
        self._load_assumptions()
        self._scan_fixups()
        self._collect_globals()
        self.notes = []
        self.funcs = []          # (proto text, start index, end index, code offset)
        self._split_functions()

    # -- symbol tables ------------------------------------------------------
    def _load_assumptions(self):
        for k, t, v in self.d.assumptions:
            if k == "S":
                m = re.match(r"^sizeof\((.*)\)$", t)
                if m:
                    self.types.set_sizeof(m.group(1), v)
        for k, t, v in self.d.assumptions:
            if k == "M":
                mt, _, rest = t.partition(" ")
                cls, _, mem = rest.partition("::")
                self.types.members[(cls, mem)] = (self.types.parse(mt), v, t)
        # reverse map: struct -> offset -> (member, type)
        self.memb_at = collections.defaultdict(dict)
        for (cls, mem), (mt, off, txt) in self.types.members.items():
            if off is not None:
                self.memb_at[cls].setdefault(off, (mem, mt))
        self.memb_at["Vector"] = {0: ("x", DC.T_FLOAT), 4: ("y", DC.T_FLOAT), 8: ("z", DC.T_FLOAT)}

    def member_path(self, cls, off):
        """`off` bytes into `cls` -> ("member[.sub]", type), following Vector/struct
        members whose interior the shipped code addresses directly."""
        tbl = self.memb_at.get(cls)
        if not tbl:
            return None
        if off in tbl:
            return tbl[off]
        for o in sorted(tbl):
            mem, mt = tbl[o]
            if not (o < off < o + max(4, mt.size)):
                continue
            sub = self.member_path(mt.txt.lstrip("@"), off - o)
            if sub:
                return ("%s.%s" % (mem, sub[0]), sub[1])
        return None

    def _scan_fixups(self):
        self.used_natives = []
        self.used_scripts = []
        self.used_enums = []
        self.used_globals = []
        seen = set()
        for k, sym, sites in self.d.fixups:
            if (k, sym) in seen:
                continue
            seen.add((k, sym))
            if k == "N":
                self.used_natives.append(sym)
            elif k == "C":
                self.used_scripts.append(sym)
            elif k == "I":
                self.used_enums.append(sym)
            elif k == "D":
                self.used_globals.append(sym)
        for sym in self.used_natives:
            try:
                self.db.add_native(sym)
            except Exception:
                pass
        for sym in self.used_enums:
            self.db.add_enum(sym)

    def _member_decls(self, db2):
        """`struct` declarations giving the compiler the member offsets this module uses."""
        used = collections.defaultdict(dict)      # cls -> member -> (typetxt, offset)
        for k, sym, sites in self.d.fixups:
            if k != "M":
                continue
            mt, _, rest = sym.partition(" ")
            cls, _, mem = rest.partition("::")
            if not cls or cls == "Vector":
                continue
            info = self.types.member(cls, mem)
            off = info[1] if info else None
            used[cls][mem] = (mt, off)
        out = []
        for cls in sorted(used):
            need = False
            for mem, (mt, off) in used[cls].items():
                known = db2.types.member(cls, mem)
                if not known or known[1] is None or known[1] != off:
                    need = True
            if not need:
                continue
            fields = sorted([kv for kv in used[cls].items() if kv[1][1] is not None],
                            key=lambda kv: kv[1][1])
            unknown = sorted(kv for kv in used[cls].items() if kv[1][1] is None)
            cur = 0
            body = []
            pad = 0
            for mem, (mt, off) in fields:
                while cur < off:
                    body.append("int _pad%d;" % pad)
                    pad += 1
                    cur += 4
                if cur != off:
                    body = None
                    break
                t = self.types.parse(mt)
                body.append("%s %s;" % (mt, mem))
                cur += max(4, t.size)
            if body is None:
                self.notes.append("cannot lay out %s (overlapping members)" % cls)
                continue
            size = None
            for k, t, v in self.d.assumptions:
                if k == "S" and t == "sizeof(%s)" % cls:
                    size = v
            if size is not None:
                while cur < size:
                    body.append("int _pad%d;" % pad)
                    pad += 1
                    cur += 4
            # Members only ever reached through a pointer have no compile-time offset,
            # so give them a synthetic one.
            for mem, (mt, _off) in unknown:
                body.append("%s %s; /* synthetic offset: not in ASSUMPTIONS */" % (mt, mem))
            out.append("struct %s { %s }" % (cls, " ".join(body)))
        return out

    def _collect_globals(self):
        self.own_globals = []           # (dataoff, typetxt, name, symtext)
        own_syms = set()
        for k, off, txt in self.d.exports:
            if k != "D":
                continue
            ty, _, nm = txt.rpartition(" ")
            self.own_globals.append((off, ty, nm, txt))
            own_syms.add(txt)
        self.own_globals.sort()
        self.ext_globals = []
        for sym in self.used_globals:
            if sym == self.d.stem_data or sym in own_syms:
                continue
            ty, _, nm = sym.rpartition(" ")
            if not ty:
                continue
            self.ext_globals.append((ty, nm, sym))
        self.global_type = {}
        for _, ty, nm, sym in self.own_globals:
            self.global_type[sym] = (ty, nm)
        for ty, nm, sym in self.ext_globals:
            self.global_type[sym] = (ty, nm)

    def _split_functions(self):
        offs = sorted(self.d.func_at)
        idx = {ins.off: i for i, ins in enumerate(self.d.code)}
        for n, off in enumerate(offs):
            nxt = offs[n + 1] if n + 1 < len(offs) else None
            i0 = idx[off]
            i1 = idx[nxt] if nxt is not None else len(self.d.code)
            self.funcs.append((self.d.func_at[off], i0, i1, off))

    # -- driver -------------------------------------------------------------
    def decompile(self):
        """Decompile every export.  Returns a list of FnResult."""
        self.results = []
        self.init_result = None
        initname = "void __%s_init()" % self.stem
        for proto, i0, i1, off in self.funcs:
            body = self.d.code[i0:i1]
            r = FnResult(proto, off, body)
            try:
                fn = Fn(self, proto, body, off)
                try:
                    fn.run()
                except Exception:
                    fn.run_goto()
                r.stmts = fn.stmts
                r.params = fn.param_decl
                r.ok = True
                r.goto_fallback = fn.used_goto
                r.goto_unsupported = fn.goto_unsupported
                r.notes = fn.notes
            except Exception as ex:                       # never crash
                r.ok = False
                r.error = "%s: %s" % (type(ex).__name__, ex)
            if proto == initname:
                self.init_result = r
            else:
                self.results.append(r)
        self._fold_init()
        return self.results

    def _fold_init(self):
        """Turn __<stem>_init()'s stores into global initialisers."""
        self.global_init = {}
        self.init_extra = []
        r = self.init_result
        if r is None or not r.ok:
            return
        names = {nm for _, _, nm, _ in self.own_globals}
        for s in r.stmts:
            if s.kind == "raw" and s.a.endswith(";") and " = " in s.a:
                lhs, _, rhs = s.a[:-1].partition(" = ")
                if lhs.strip() in names and lhs.strip() not in self.global_init:
                    self.global_init[lhs.strip()] = rhs.strip()
                    continue
            if s.kind == "comment":
                continue
            self.init_extra.append(s)

    # -- source emission ----------------------------------------------------
    def source(self, compilable=False, stub=(), drop=()):
        """Render the whole module.  `stub` = protos to emit as empty bodies."""
        L = []
        A = L.append
        A("// Decompiled from %s by `dante decompile` -- see docs/dante_lang.md" % os.path.basename(self.path))
        A("// %d exports, %d instructions, %d words of code, %d bytes of data."
          % (len(self.funcs), len(self.d.code), self.d.code_size, self.d.data_size))
        A("")
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", self.stem):
            A("module %s;" % self.stem)
        else:
            A("// module %s;   <- not a `.dn` identifier: compile with"
              " `dante compile --module %s`" % (self.stem, self.stem))
        A("")
        # natives the shared symbol DB does not already carry
        known = {p.text for p in self.db.natives}
        pre = set()
        for kind, p in DC.default_symbol_paths():
            pass
        db2 = DC.SymbolDB(DC.Types())
        for kind, p in DC.default_symbol_paths():
            (db2.load_api if kind == "api" else db2.load_json)(p)
        pre = {p.text for p in db2.natives}
        newnat = [n for n in self.used_natives if n not in pre]
        if newnat:
            A("// ---- natives not in dante_symbols.json")
            for n in sorted(newnat):
                A('native "%s";' % n)
            A("")
        mdecls = self._member_decls(db2)
        if mdecls:
            A("// ---- struct/class member offsets used by this module (from ASSUMPTIONS)")
            for m in mdecls:
                A(m)
            A("")
        own_protos = {p for p, _, _, _ in self.funcs} - set(drop)
        ext = [s for s in self.used_scripts if s not in own_protos]
        ext += [p for p in drop if p in self.used_scripts or True]
        if ext:
            A("// ---- script functions exported by other modules")
            for n in sorted(set(ext)):
                A('extern "%s";' % n)
            A("")
        enums = collections.defaultdict(list)
        for e in self.used_enums:
            parts = e.split()
            if len(parts) == 2:
                enums[parts[0]].append(parts[1])
        if enums:
            A("// ---- enum constants used by this module")
            for et in sorted(enums):
                A("enum %s { %s }" % (et, ", ".join(sorted(set(enums[et])))))
            A("")
        if self.ext_globals:
            A("// ---- globals owned by the engine or another module")
            for ty, nm, sym in sorted(self.ext_globals, key=lambda g: g[1]):
                A("extern %s %s;" % (ty, ident(nm)))
            A("")
        if self.own_globals:
            A("// ---- this module's globals (data offsets %04X..)" % self.own_globals[0][0])
            for off, ty, nm, sym in self.own_globals:
                init = getattr(self, "global_init", {}).get(nm)
                if init is not None:
                    A("%s %s = %s;" % (ty, ident(nm), init))
                else:
                    A("%s %s;" % (ty, ident(nm)))
            A("")
        for st in getattr(self, "init_extra", []):
            for ln in render([st], 0):
                A("// __%s_init: %s" % (self.stem, ln.strip()))
        if getattr(self, "init_extra", []):
            A("")
        self.line_of = {}
        for r in self.results:
            if compilable and r.proto in drop:
                continue
            _ln0 = len(L) + 1
            A("// @%X" % r.off)
            if not r.ok:
                A("// !! dantedec could not structure this function: %s" % r.error)
            elif r.goto_fallback:
                A("// !! irreducible control flow -- labelled goto fallback")
                if r.goto_unsupported:
                    A("// !! and an exception scope that does not fold back into"
                      " try/catch -- not compilable")
            head = r.signature(self)
            if (not r.ok or (compilable and r.proto in stub)):
                A(head)
                A("{")
                if not r.ok:
                    A("    // ---- raw disassembly ----")
                    for ins in r.body:
                        A("    // %5X: %s" % (ins.off, self.d.fmt_instr(ins)))
                else:
                    A("    // (body omitted: it does not recompile)")
                A("}")
            else:
                A(head)
                A("{")
                L.extend(render(r.stmts, 1))
                A("}")
            A("")
            self.line_of[r.proto] = (_ln0, len(L))
        return "\n".join(L) + "\n"


class FnResult:
    def __init__(self, proto, off, body):
        self.proto = proto
        self.off = off
        self.body = body
        self.stmts = []
        self.params = []
        self.ok = False
        self.error = ""
        self.goto_fallback = False
        self.goto_unsupported = False     # an exception scope that will not fold back
        self.notes = []

    def signature(self, mod):
        p = DC.Proto(self.proto, mod.types)
        args = ", ".join("%s %s" % (t.txt, n) for t, n in self.params) if self.params else \
               ", ".join("%s a%d" % (t.txt, i) for i, t in enumerate(p.params))
        return "%s %s(%s)" % (p.ret.txt, p.name, args)


# ==========================================================================
NAME_HINT = {"int": "n", "float": "f", "bool": "b", "String": "s", "Vector": "v"}


class _FnPtrProto:
    """A call through a function-value global -- `.dn` cannot spell this."""
    __slots__ = ("name", "ret", "params", "this", "cls", "text", "indirect")

    def __init__(self, name, ftype, text):
        self.name = name
        self.ret = ftype.ret
        self.params = list(ftype.params or [])
        self.this = None
        self.cls = None
        self.text = text
        self.indirect = True


class Fn:
    """Decompiles one exported function."""

    def __init__(self, mod, proto_text, body, off):
        self.m = mod
        self.d = mod.d
        self.types = mod.types
        self.proto = DC.Proto(proto_text, self.types)
        self.ins = body
        self.off = off
        self.n = len(body)
        self.notes = []
        self.used_goto = False
        self.goto_unsupported = False
        self.stmts = []
        self.pending = {}
        self.names = {}                # slot -> name
        self.slot_type = {}            # slot -> Type
        self.declared = set()
        self.struct_at = {}            # base slot -> (name, struct Type)
        self.struct_at_end = {}        # base slot -> index its DTORPOP releases it
        self.skip = set()
        self.used_names = set()
        self.labels = {}
        self.autodecl = []
        self.next_slot = 8
        self._peeked = set()
        self.retype = {}               # slot -> [(start index, Type)] web groups
        self._webcur = {}
        self._webnames = {}
        self._webdecl = {}
        self._prep()

    # ----------------------------------------------------------------- setup
    def _prep(self):
        idx = {ins.off: i for i, ins in enumerate(self.ins)}
        self.tgt = [None] * self.n
        self.targets = set()
        for i, ins in enumerate(self.ins):
            if is_jump(ins):
                t = ins.off + s16(ins.ops[0].words[0]) if ins.ops[0].nwords else ins.off
                if t not in idx:
                    raise Bail("jump out of the function at %X" % ins.off)
                self.tgt[i] = idx[t]
                self.targets.add(idx[t])
        self.has_try = any(ins.name in ("TRY", "ENDTRY") for ins in self.ins)
        if not self.ins:
            raise Bail("empty function")
        # (a) the epilogue is the trailing RET; a function that ends in an infinite
        # loop has none
        if self.ins[-1].op == NAMEOP["RET"]:
            self.epi = self.n - 1
            self.body_end = self.n - 1
        else:
            self.epi = None
            self.body_end = self.n
        # parameter / return slots (mirrors the compiler's FuncGen)
        sizes = [t.size for t in self.proto.params]
        total = sum(sizes)
        acc = 0
        self.param_slots = []
        for i, t in enumerate(self.proto.params):
            off = -(total - acc)
            acc += sizes[i]
            self.param_slots.append(off)
            self.slot_type[off] = t
        self.ret_slot = None
        if self.proto.ret.kind != "void":
            self.ret_slot = -(total + self.proto.ret.size)
            self.slot_type[self.ret_slot] = self.proto.ret
        self.param_decl = []
        for i, t in enumerate(self.proto.params):
            nm = self._mkname(t, "a%d" % i)
            self.names[self.param_slots[i]] = nm
            self.param_decl.append((t, nm))
        self._calls()
        self._liveness()
        self._prescan()
        self._referenced()
        self._wants()
        self._webs()
        self._infer_types()

    def _mkname(self, t, fallback):
        base = None
        if t is None:
            base = "x"
        elif t.kind in NAME_HINT:
            base = NAME_HINT[t.kind]
        elif t.kind in ("obj", "cls") and t.inner:
            inner = t.inner
            base = inner[1:] if re.match(r"^C[A-Z]", inner) else inner
            base = base[0].lower() + base[1:]
        elif t.kind == "struct":
            base = t.txt[1:]
            base = base[0].lower() + base[1:]
        elif t.kind == "ptr" and t.inner:
            base = "p" + t.inner.lstrip("@")
        elif t.kind == "enum":
            base = "e"
        else:
            base = "x"
        base = ident(base)
        if base not in self.used_names:
            self.used_names.add(base)
            return base
        for k in range(1, 99):
            c = "%s%d" % (base, k)
            if c not in self.used_names:
                self.used_names.add(c)
                return c
        self.used_names.add(fallback)
        return fallback

    # call blocks -----------------------------------------------------------
    def _calls(self):
        self.call = {}          # index -> (proto, is_native, base, end, retoff, thisoff, argoffs)
        for i, ins in enumerate(self.ins):
            if ins.op != NAMEOP["CALL"]:
                continue
            fx = ins.ops[0].fix
            if not fx:
                continue
            kind, text = fx[0]
            if kind not in ("N", "C", "D"):
                continue
            if kind == "D":
                # an indirect call through a function-value global
                g = self.m.global_type.get(text)
                if not g:
                    continue
                ft = self.types.parse(g[0])
                if ft.kind != "func":
                    continue
                p = _FnPtrProto(ident(g[1]), ft, text)
            else:
                try:
                    p = DC.Proto(text, self.types)
                except Exception:
                    continue
            base = ins.ops[2].value()
            end = ins.ops[1].value()
            cur = base
            retoff = thisoff = None
            if p.ret.kind != "void":
                retoff = cur
                cur += p.ret.size
            if kind == "N" and p.this is not None:
                thisoff = cur
                cur += 4
            argoffs = []
            for t in p.params:
                argoffs.append(cur)
                cur += t.size
            self.call[i] = (p, kind == "N", base, end, retoff, thisoff, argoffs)

    # liveness --------------------------------------------------------------
    def _du(self, i):
        """(defs, uses) as sets of 4-byte slot offsets."""
        ins = self.ins[i]
        nm = ins.name
        defs, uses = set(), set()
        if nm in ("STRFREE", "DTORPOP", "CTOR", "ENTER"):
            return defs, uses
        if i in self.call:
            p, native, base, end, retoff, thisoff, argoffs = self.call[i]
            if retoff is not None:
                for k in range(p.ret.size // 4):
                    defs.add(retoff + 4 * k)
            if base >= 0:
                cur = base + (p.ret.size if retoff is not None else 0)
                while cur < end:
                    uses.add(cur)
                    cur += 4
            return defs, uses
        spec = OPS[ins.op]
        for idx in range(3):
            o = ins.ops[idx]
            role = spec[idx + 1]
            if o.mode == 3:
                v = s16(o.words[0])
                if idx == 0 and role == "d":
                    sz = 12 if nm in ("MOVV", "ADDV", "SUBV", "MULVS", "DIVVS") else 4
                    if nm == "THREAD":
                        sz = 8
                    for k in range(sz // 4):
                        defs.add(v + 4 * k)
                else:
                    sz = 4
                    if nm in ("MOVV", "ADDV", "SUBV", "DOT", "VTOS") or \
                       (nm in ("MULVS", "DIVVS") and idx == 1) or \
                       (nm in ("JEQ_V", "JNE_V", "EQ_V", "NE_V")):
                        sz = 12
                    for k in range(sz // 4):
                        uses.add(v + 4 * k)
            elif o.mode == 7:
                uses.add(s16(o.words[0]))
        if nm == "LEA" and ins.ops[1].mode == 3:
            uses.add(s16(ins.ops[1].words[0]))
        return defs, uses

    def _liveness(self):
        leaders = {0}
        for i, ins in enumerate(self.ins):
            if is_jump(ins):
                leaders.add(self.tgt[i])
                if i + 1 < self.n:
                    leaders.add(i + 1)
        starts = sorted(leaders)
        self.blk_of = [0] * self.n
        blocks = []
        for k, s in enumerate(starts):
            e = starts[k + 1] if k + 1 < len(starts) else self.n
            blocks.append((s, e))
            for i in range(s, e):
                self.blk_of[i] = k
        self.blocks = blocks
        succ = [[] for _ in blocks]
        bid = {s: k for k, (s, e) in enumerate(blocks)}
        for k, (s, e) in enumerate(blocks):
            last = self.ins[e - 1]
            if last.op == NAMEOP["RET"]:
                continue
            if is_jump(last):
                succ[k].append(bid[self.tgt[e - 1]])
                if not is_goto(last) and e < self.n:
                    succ[k].append(bid.get(e, k))
            elif e < self.n:
                succ[k].append(bid.get(e, k))
        self.du = [self._du(i) for i in range(self.n)]
        nb = len(blocks)
        live_in = [set() for _ in range(nb)]
        live_out = [set() for _ in range(nb)]
        for _ in range(nb + 4):
            changed = False
            for k in range(nb - 1, -1, -1):
                out = set()
                for t in succ[k]:
                    out |= live_in[t]
                s0, e0 = blocks[k]
                live = set(out)
                for i in range(e0 - 1, s0 - 1, -1):
                    dd, uu = self.du[i]
                    live -= dd
                    live |= uu
                if out != live_out[k] or live != live_in[k]:
                    live_out[k] = out
                    live_in[k] = live
                    changed = True
            if not changed:
                break
        self.live_out = live_out
        self.succ = succ

    def _prescan(self):
        """Slots that must be locals: struct locals and component-written Vectors."""
        self.forced_local = {}          # slot -> first instruction it is a local at
        self.block_slots = set()
        self.struct_pre = {}
        self.struct_ranges = {}         # slot -> [(ctor index, dtorpop index)]
        self._struct_ctor_end = {}      # ctor LEA index -> dtorpop index
        nb = len(self.blocks)
        self.block_minbase = [0x7FFF] * nb
        self.block_ret = [set() for _ in range(nb)]
        for c, (p, native, base, end, retoff, thisoff, argoffs) in self.call.items():
            k = self.blk_of[c]
            if base >= 0:
                self.block_minbase[k] = min(self.block_minbase[k], base)
            if retoff is not None:
                self.block_ret[k].add(retoff)
        for c, (p, native, base, end, retoff, thisoff, argoffs) in self.call.items():
            if base >= 0:
                for o in range(base, end, 4):
                    self.block_slots.add(o)
        # String/struct locals are the ones released at scope exit, as a trailing run
        # of STRFREE/DTORPOP.  Any other STRNEW slot is a temporary.
        self.scope_free = set()
        for s0, e0 in self.blocks:
            j = e0 - 1
            if j >= s0 and (is_jump(self.ins[j]) or self.ins[j].op == NAMEOP["RET"]):
                j -= 1
            while j >= s0 and self.ins[j].name in ("STRFREE", "DTORPOP"):
                o = self.ins[j].ops[0]
                if self.ins[j].name == "STRFREE" and o.mode == 3:
                    self.scope_free.add(s16(o.words[0]))
                j -= 1

        for i, ins in enumerate(self.ins):
            if ins.name != "LEA" or ins.ops[0].mode != 3 or ins.ops[1].mode != 3:
                continue
            if s16(ins.ops[0].words[0]) != s16(ins.ops[1].words[0]):
                continue
            if (i + 1) not in self.call:
                continue
            p = self.call[i + 1][0]
            if p.name != "constructor" or not p.cls:
                continue
            slot = s16(ins.ops[0].words[0])
            st = self.types.parse(p.cls)
            self.struct_pre[slot] = st
            # A struct's slots are members only between its ctor and the DTORPOP that
            # releases them.  Early returns also emit DTORPOPs, so take the last one.
            end = self.n
            for j in range(i + 1, self.n):
                nx = self.ins[j]
                if nx.name == "LEA" and nx.ops[0].mode == 3 and nx.ops[1].mode == 3 and \
                        s16(nx.ops[0].words[0]) == slot == s16(nx.ops[1].words[0]) and \
                        (j + 1) in self.call and self.call[j + 1][0].name == "constructor":
                    break
                if nx.name == "DTORPOP" and nx.ops[0].mode == 3 and \
                        s16(nx.ops[0].words[0]) == slot:
                    end = j
            self._struct_ctor_end[i] = end
            for o in range(slot, slot + max(4, st.size), 4):
                self.struct_ranges.setdefault(o, []).append((i, end))

    def _vector_locals(self):
        vec = {s for s, t in self.slot_type.items() if t is not None and t.kind == "Vector"}
        # a Vector lane is a float: only an instruction that can produce one counts
        # as a component write (a LEA/DYNCAST/String temp that lands on b+4 is not)
        NARROW = ("MOV", "ADDF", "SUBF", "MULF", "DIVF", "MODF", "ITOF", "DOT", "FTOI")
        narrow = set()
        for i, ins in enumerate(self.ins):
            if ins.ops[0].mode == 3 and OPS[ins.op][1] == "d" and ins.name in NARROW:
                narrow.add(s16(ins.ops[0].words[0]))
        self.vec_base = set()
        for b in vec:
            if b < 8:
                self.vec_base.add(b)
                continue
            blocked = b in self.block_slots or (b + 4) in self.block_slots
            if blocked and not (narrow & {b, b + 4, b + 8}):
                continue
            if blocked:
                # Call storage earlier on; the Vector local starts at the first lane
                # write after the last call covering it.
                first = None
                for c, cc in self.call.items():
                    if cc[2] >= 0 and cc[2] < b + 12 and b < cc[3]:
                        first = c + 1 if first is None else max(first, c + 1)
                if first is None:
                    continue
                if any(sb <= b < sb + max(4, st.size)
                       for sb, st in self.struct_pre.items()):
                    continue                 # a struct local, not a Vector
                if not any(self.ins[i].name in NARROW and
                           self.ins[i].ops[0].mode == 3 and
                           s16(self.ins[i].ops[0].words[0]) == b
                           for i in range(first, self.n)):
                    continue
                self.vec_base.add(b)
                for o in (b, b + 4, b + 8):
                    self.forced_local[o] = min(self.forced_local.get(o, 1 << 30), first)
                continue
            if narrow & {b, b + 4, b + 8}:
                self.vec_base.add(b)
                for o in (b, b + 4, b + 8):
                    self.forced_local[o] = 0
            else:
                self.vec_base.add(b)

    def _wants(self):
        """instruction index -> the type its destination is being filled with."""
        self.want_of = {}
        for c in sorted(self.call):
            p, native, base, end, retoff, thisoff, argoffs = self.call[c]
            remaining = {}
            if thisoff is not None:
                remaining[thisoff] = p.this
            for o, t in zip(argoffs, p.params):
                remaining[o] = t
            s0, e0 = self.blocks[self.blk_of[c]]
            for j in range(c - 1, s0 - 1, -1):
                if not remaining:
                    break
                ins = self.ins[j]
                if ins.ops[0].mode == 3 and OPS[ins.op][1] == "d":
                    sl = s16(ins.ops[0].words[0])
                    if sl in remaining:
                        self.want_of[j] = remaining.pop(sl)

    def _referenced(self):
        ref = set()
        for i, ins in enumerate(self.ins):
            dd, uu = self._du(i)
            ref |= dd
            ref |= uu
            for o in ins.ops:
                if o.mode == 3:
                    ref.add(s16(o.words[0]))
                elif o.mode == 7:
                    ref.add(s16(o.words[0]))
        for b, st in self.struct_pre.items():
            for k in range(0, max(4, st.size), 4):
                ref.add(b + k)
        self.referenced = ref

    def _webs(self):
        """Group a slot's definitions into webs: those reaching a common use are one
        variable.  One slot can hold differently-typed variables at different times."""
        self.web_groups = {}           # slot -> [(start, [defs], [uses]), ...]
        self.web_of = {}               # (slot, instr index) -> web index
        cand = {}
        VEC = {"MOVV", "ADDV", "SUBV", "MULVS", "DIVVS", "DOT", "VTOS",
               "JEQ_V", "JNE_V", "EQ_V", "NE_V"}
        vec_touched = set()
        for i in range(self.n):
            dd, uu = self.du[i]
            if self.ins[i].name in VEC:
                vec_touched |= dd | uu
            for o in dd:
                cand.setdefault(o, (set(), set()))[0].add(i)
            for o in uu:
                cand.setdefault(o, (set(), set()))[1].add(i)
        nb = len(self.blocks)
        preds = [[] for _ in range(nb)]
        for k in range(nb):
            for t in self.succ[k]:
                preds[t].append(k)
        for slot, (defs, uses) in sorted(cand.items()):
            if slot < 8 or slot in vec_touched or slot in self.struct_ranges or \
                    slot in self.forced_local:
                continue
            kind = {d: self.classify(d, slot) for d in defs}
            local = sorted(d for d in defs if kind[d] == "local")
            if len(local) < 2:
                continue
            # block-level reaching definitions; a temp/dead definition kills
            # but generates nothing
            gen = {}
            for k, (s0, e0) in enumerate(self.blocks):
                last = None
                for j in range(s0, e0):
                    if j in defs:
                        last = j
                if last is not None:
                    gen[k] = {last} if kind.get(last) == "local" else set()
            rin = [set() for _ in range(nb)]
            rout = [set() for _ in range(nb)]
            for _ in range(nb + 4):
                changed = False
                for k in range(nb):
                    ri = set()
                    for p in preds[k]:
                        ri |= rout[p]
                    ro = gen[k] if k in gen else ri
                    if ri != rin[k] or ro != rout[k]:
                        rin[k], rout[k] = ri, ro
                        changed = True
                if not changed:
                    break
            parent = {d: d for d in local}

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            use_web = {}
            ok = True
            for u in sorted(uses):
                k = self.blk_of[u]
                lastdef = None
                for j in range(self.blocks[k][0], u):
                    if j in defs:
                        lastdef = j
                if lastdef is not None:
                    if kind.get(lastdef) != "local":
                        continue           # consumes a temporary, not a variable
                    reach = {lastdef}
                else:
                    reach = rin[k]
                    if not reach:
                        ok = False         # read before any store: leave it alone
                        break
                rl = sorted(reach)
                r0 = find(rl[0])
                for d in rl[1:]:
                    parent[find(d)] = r0
                use_web[u] = r0
            if not ok:
                continue
            webs = {}
            for d in local:
                webs.setdefault(find(d), []).append(d)
            if len(webs) < 2:
                continue
            groups = []
            for root, ds in webs.items():
                us = sorted(u for u, r in use_web.items() if find(r) == root)
                groups.append((min(ds), sorted(ds), us))
            groups.sort()
            # program-order sanity: a web must end before the next one starts,
            # or renaming from the split point on would be wrong
            over = False
            for a in range(len(groups) - 1):
                if max(groups[a][1] + groups[a][2]) >= groups[a + 1][0]:
                    over = True
                    break
            if over:
                continue
            self.web_groups[slot] = groups
            for w, (st, ds, us) in enumerate(groups):
                for j in ds + us:
                    self.web_of[(slot, j)] = w

    def _fills_block(self, i, slot):
        """True when this write fills a slot of a call block that runs later in the
        same basic block -- that is call storage, never a local."""
        if slot not in self.block_slots:
            return False
        k = self.blk_of[i]
        s0, e0 = self.blocks[k]
        for c in range(i + 1, e0):
            if c not in self.call:
                continue
            p, nat, base, end, ro, to, ao = self.call[c]
            if base >= 0 and base <= slot < end:
                return True
        return False

    def _ptr_only(self, i, slot):
        """True when what was MOVed into `slot` is only ever used as the base of a
        mode-7 operand: a pointer materialisation, not a variable."""
        k = self.blk_of[i]
        s0, e0 = self.blocks[k]
        seen = 0
        redef = False
        for j in range(i + 1, e0):
            ins = self.ins[j]
            for idx in range(3):
                o = ins.ops[idx]
                if idx == 0 and OPS[ins.op][1] == "d":
                    continue                 # the destination is not a use
                if o.mode == 3 and s16(o.words[0]) == slot:
                    return False
                if o.mode == 7 and s16(o.words[0]) == slot:
                    seen += 1
            if slot in self.du[j][0]:
                redef = True
                break
        if not redef and slot in self.live_out[k]:
            return False
        return seen > 0

    def classify(self, i, slot):
        """'dead' | 'temp' | 'local' for the definition of `slot` at index i."""
        if slot < 8 or i >= self.forced_local.get(slot, 1 << 30) or \
                any(s <= i < e for s, e in self.struct_ranges.get(slot, ())):
            return "local"
        # R1: a plain copy into a slot that isn't filling a call block defines a local.
        # The compiler never copies one temporary into another.
        ins = self.ins[i]
        if ins.name in ("MOV", "MOVV", "STRCPY") and ins.ops[0].mode == 3 and \
                s16(ins.ops[0].words[0]) == slot and slot != self.ret_slot and \
                i not in self.want_of and not self._fills_block(i, slot) and \
                not self._ptr_only(i, slot):
            return "local"
        if ins.ops[0].mode == 3 and s16(ins.ops[0].words[0]) == slot and \
                self._fills_block(i, slot):
            return "temp"
        k = self.blk_of[i]
        s, e = self.blocks[k]
        uses = 0
        redefined = False
        for j in range(i + 1, e):
            dd, uu = self.du[j]
            nx = self.ins[j]
            selfptr = (nx.name == "LEA" and nx.ops[0].mode == 3 and nx.ops[1].mode == 3 and
                       s16(nx.ops[0].words[0]) == s16(nx.ops[1].words[0]) == slot)
            if slot in uu and not selfptr:
                uses += 1                    # `LEA d, d` takes an address, not a value
            if slot in dd:
                redefined = True
                break
        live = (slot in self.live_out[k]) and not redefined
        if uses == 0 and not live:
            return "dead"
        if uses == 1 and not live:
            # A call result stored below the temp region is a named local: temporaries
            # always start at locals_end.
            ins = self.ins[i]
            if ins.name in ("MOV", "MOVV", "STRCPY") and ins.ops[0].mode == 3 and \
                    ins.ops[1].mode == 3 and slot < self.block_minbase[k] and \
                    s16(ins.ops[1].words[0]) in self.block_ret[k]:
                return "local"
            return "temp"
        return "local"

    # type inference --------------------------------------------------------
    def _settype(self, slot, t, prio, at=None):
        if t is None or t.kind == "void":
            return
        if at is not None and slot in self.web_groups:
            w = self.web_of.get((slot, at))
            if w is not None and prio > self._webprio.get((slot, w), -1):
                self._webtype[(slot, w)] = t
                self._webprio[(slot, w)] = prio
        old = self.slot_type.get(slot)
        oldp = self._tprio.get(slot, -1)
        if old is None or prio > oldp:
            self.slot_type[slot] = t
            self._tprio[slot] = prio

    def _infer_types(self):
        """Type the slots that behave as locals; a temporary's type says nothing."""
        self._tprio = {}
        self._webtype = {}
        self._webprio = {}
        for sl in list(self.slot_type):
            self._tprio[sl] = 100
        for s0, e0 in self.blocks:
            cls_of = {}
            tmp_type = {}              # temp slot -> (Type, prio) its value carries
            for i in range(s0, e0):
                ins = self.ins[i]
                nm = ins.name
                # ---- uses
                def use(slot, t, prio):
                    if t is None:
                        return
                    if cls_of.get(slot, "local") == "local":
                        self._settype(slot, t, prio, at=i)
                if nm in ("MOV", "MOVV", "STRCPY") and ins.ops[1].mode == 3:
                    src0 = s16(ins.ops[1].words[0])
                    if self.ret_slot is not None and ins.ops[0].mode == 3 and \
                            s16(ins.ops[0].words[0]) == self.ret_slot:
                        use(src0, self.proto.ret, 80)
                w = self.want_of.get(i)
                if w is not None and ins.ops[1].mode == 3 and nm in ("MOV", "MOVV", "STRCPY", "LEA"):
                    if nm == "LEA" and w.kind == "ptr" and w.inner:
                        use(s16(ins.ops[1].words[0]), self.types.parse(w.inner), 80)
                    elif nm != "LEA":
                        use(s16(ins.ops[1].words[0]), w, 80)
                if nm in SRC_TYPE:
                    for k in (1, 2):
                        if ins.ops[k].mode == 3 and OPS[ins.op][k + 1] == "r":
                            t = self.types.parse(SRC_TYPE[nm])
                            if t.kind == "Vector" and not (nm in ("MULVS", "DIVVS") and k == 2):
                                self._settype(s16(ins.ops[k].words[0]), t, 88)
                            else:
                                use(s16(ins.ops[k].words[0]), t, 60)
                if nm in ("MOVV", "ADDV", "SUBV", "MULVS", "DIVVS") and ins.ops[0].mode == 3:
                    self._settype(s16(ins.ops[0].words[0]), DC.T_VEC, 88)
                if nm in ("JEQ_V", "JNE_V", "EQ_V", "NE_V"):
                    for k in (1, 2):
                        if ins.ops[k].mode == 3:
                            self._settype(s16(ins.ops[k].words[0]), DC.T_VEC, 88)
                if nm in ("MULVS", "DIVVS") and ins.ops[2].mode == 3:
                    use(s16(ins.ops[2].words[0]), DC.T_FLOAT, 60)
                if nm in JCMP_TYPE:
                    for k in (1, 2):
                        if ins.ops[k].mode == 3:
                            use(s16(ins.ops[k].words[0]), self.types.parse(JCMP_TYPE[nm]), 60)
                if nm in ("MOV", "MOVV", "STRCPY") and ins.ops[0].mode == 5 and ins.ops[0].fix \
                        and ins.ops[1].mode == 3:
                    g = self.m.global_type.get(ins.ops[0].fix[0][1])
                    if g and len(ins.ops[0].fix) == 1:
                        gt = self.types.parse(g[0])
                        if gt.kind == "struct":
                            mem = self.m.member_path(gt.txt, s32(*ins.ops[0].words))
                            if mem is not None:
                                gt = mem[1]
                        use(s16(ins.ops[1].words[0]), gt, 70)
                for k in range(3):
                    o = ins.ops[k]
                    if o.mode != 7:
                        continue
                    bslot = s16(o.words[0])
                    if o.fix16:
                        mt, _, rest = o.fix16[0][1].partition(" ")
                        cls = rest.partition("::")[0]
                        if cls and cls != "Vector":
                            self._settype(bslot, self.types.parse("@" + cls), 92, at=i)
                    else:
                        vt = None
                        if k == 0 and nm in DEST_TYPE:
                            vt = DEST_TYPE[nm]
                        elif k > 0 and nm in SRC_TYPE:
                            vt = SRC_TYPE[nm]
                        elif nm in JCMP_TYPE:
                            vt = JCMP_TYPE[nm]
                        elif nm in ("MOV",) and k == 1:
                            w2 = self.want_of.get(i)
                            vt = w2.txt if w2 is not None else None
                        if vt in ("int", "float", "bool", "String"):
                            use(bslot, self.types.parse("@" + vt), 92)
                        elif vt is None:
                            use(bslot, self.types.parse("@int"), 20)
                # ---- definitions
                dd, _ = self.du[i]
                dslot = s16(ins.ops[0].words[0]) if (ins.ops[0].mode == 3 and OPS[ins.op][1] == "d") else None
                if i in self.call:
                    p, native, base, end, retoff, thisoff, argoffs = self.call[i]
                    if retoff is not None:
                        c = self.classify(i, retoff)
                        cls_of[retoff] = c
                        if c == "local":
                            self._settype(retoff, p.ret, 90, at=i)
                        else:
                            tmp_type[retoff] = (p.ret, 90)
                    for o in dd:
                        cls_of.setdefault(o, "temp")
                    continue
                if dslot is None:
                    continue
                c = self.classify(i, dslot)
                for o in dd:
                    cls_of[o] = c
                vt = None                  # (Type, prio) this definition carries
                if nm == "DYNCAST":
                    cn = DV.class_table().get(ins.ops[2].value() & 0xFFFFFFFF)
                    if cn:
                        vt = (self.types.parse("@" + cn), 95)
                elif nm in DEST_TYPE:
                    vt = (self.types.parse(DEST_TYPE[nm]), 75)
                elif nm in ("MOV", "LEA"):
                    src = ins.ops[1]
                    if src.mode == 5 and src.fix:
                        fixd = [f for f in src.fix if f[0] == "D"]
                        fixm = [f for f in src.fix if f[0] == "M"]
                        if fixm:
                            mt = fixm[0][1].partition(" ")[0]
                            vt = (self.types.parse(mt), 85)
                        elif fixd and fixd[0][1] != self.d.stem_data:
                            g = self.m.global_type.get(fixd[0][1])
                            if g:
                                gt = self.types.parse(g[0])
                                if nm == "LEA":
                                    gt = self.types.parse("@" + gt.txt.lstrip("@"))
                                vt = (gt, 85)
                    elif src.mode == 4 and src.fix and src.fix[0][0] == "I":
                        vt = (self.types.parse(src.fix[0][1].split()[0]), 85)
                    elif src.mode == 7 and src.fix16:
                        mt = src.fix16[0][1].partition(" ")[0]
                        vt = (self.types.parse(mt), 85)
                    elif src.mode == 3:
                        sslot = s16(src.words[0])
                        if nm == "MOV" and sslot in tmp_type:
                            # A value arriving through a temporary names the local's
                            # type.  Kept under 88 so a lane write can't split a Vector.
                            t0, p0 = tmp_type[sslot]
                            vt = (t0, min(p0, 87))
                        else:
                            st = self.slot_type.get(sslot)
                            if st is not None and cls_of.get(sslot, "local") == "local":
                                vt = (st, 55)
                if c == "local":
                    if vt is not None:
                        self._settype(dslot, vt[0], vt[1], at=i)
                elif vt is not None:
                    tmp_type[dslot] = vt
        FAM = {"int": "num", "bool": "num", "enum": "num", "float": "num",
               "obj": "ref", "cls": "ref", "ptr": "ref", "func": "ref"}
        for slot, groups in self.web_groups.items():
            merged = []                # [start, Type, prio, family]
            for w, (st_i, _ds, _us) in enumerate(groups):
                t = self._webtype.get((slot, w))
                p = self._webprio.get((slot, w), -1)
                f = FAM.get(t.kind) if t is not None else None
                if merged:
                    lt, lp, lf = merged[-1][1], merged[-1][2], merged[-1][3]
                    if not (f is not None and lf is not None and f != lf and
                            p >= 60 and lp >= 60):
                        if t is not None and (lt is None or p > lp):
                            merged[-1][1:4] = [t, p, f]
                        continue
                merged.append([st_i, t, p, f])
            if len(merged) < 2:
                continue
            self.retype[slot] = [(m[0], m[1]) for m in merged]
            if merged[0][1] is not None:
                self.slot_type[slot] = merged[0][1]
                self._tprio[slot] = merged[0][2]
        self._vector_locals()
        for sl, t in list(self.slot_type.items()):
            if t is not None and t.kind == "Vector" and sl not in self.vec_base:
                del self.slot_type[sl]

    # ------------------------------------------------------------- rendering
    def type_of(self, slot):
        self._web_sync(slot)
        return self.slot_type.get(slot)

    def _web_sync(self, slot):
        """Switch the slot's rendered name/type/declaredness to the web that the
        instruction being decoded (`self.cur`) falls in."""
        groups = self.retype.get(slot)
        if not groups:
            return
        i = getattr(self, "cur", None)
        if i is None:
            return
        g = 0
        for k in range(len(groups) - 1, -1, -1):
            if i >= groups[k][0]:
                g = k
                break
        old = self._webcur.get(slot, 0)
        if g == old:
            return
        self._webnames[(slot, old)] = self.names.pop(slot, None)
        self._webdecl[(slot, old)] = slot in self.declared
        nm = self._webnames.get((slot, g))
        if nm is not None:
            self.names[slot] = nm
        if self._webdecl.get((slot, g)):
            self.declared.add(slot)
        else:
            self.declared.discard(slot)
        t = groups[g][1]
        if t is not None:
            self.slot_type[slot] = t
        else:
            self.slot_type.pop(slot, None)
        self._webcur[slot] = g

    def _struct_live(self, base):
        end = self.struct_at_end.get(base, 1 << 30)
        i = getattr(self, "cur", None)
        return i is None or i < end

    def _in_struct(self, slot):
        for base, (nm, st) in self.struct_at.items():
            if base <= slot < base + max(4, st.size) and self._struct_live(base):
                return True
        return False

    def name_for(self, slot):
        self._web_sync(slot)
        if slot in self.names:
            return self.names[slot]
        t = self.type_of(slot)
        nm = self._mkname(t, "v%X" % (slot & 0xFFFF))
        self.names[slot] = nm
        return nm

    def var_ref(self, slot, width=4):
        """Render a stack slot as source: a name, or `name.member`."""
        self._web_sync(slot)
        for base, (nm, st) in self.struct_at.items():
            if base <= slot < base + max(4, st.size) and self._struct_live(base):
                if slot == base and (st.size <= 4 or width >= st.size):
                    return Ex(nm, P_ATOM, st)
                mem = self.m.member_path(st.txt, slot - base)
                if mem:
                    return Ex("%s.%s" % (nm, mem[0]), P_ATOM, mem[1])
                if slot == base:
                    return Ex(nm, P_ATOM, st)
                return Ex("%s /* +%d */" % (nm, slot - base), P_ATOM, None)
        for b in (0, 4, 8):
            base = slot - b
            if base in self.vec_base and not (b == 0 and width >= 12):
                if b == 0 and width >= 12:
                    break
                return Ex("%s.%s" % (self.name_for(base), "xyz"[b // 4]), P_ATOM, DC.T_FLOAT)
            if base in self.vec_base:
                return Ex(self.name_for(base), P_ATOM, DC.T_VEC)
        return Ex(self.name_for(slot), P_ATOM, self.type_of(slot))

    def const_ex(self, v, want, mode):
        t = want
        k = t.kind if t is not None else None
        if k == "float":
            return Ex(fmt_float(v) if mode == 4 else ("%d.0" % v if v >= 0 else "-%d.0" % -v),
                      P_ATOM if v >= 0 else P_UN, DC.T_FLOAT)
        if k == "bool":
            if v in (0, 1):
                return Ex("true" if v else "false", P_ATOM, DC.T_BOOL)
        if k in ("obj", "ptr", "func", "cls") and v == 0:
            return Ex("null", P_ATOM, t)
        if k == "String" and v == 0:
            return Ex('""', P_ATOM, DC.T_STRING)
        if mode == 4 and (t is None or k in ("int", "enum")) and abs(v) > 0x100000:
            f = bitsf(v)
            if f == f and abs(f) < 1e9 and abs(f) > 1e-6:
                return Ex("%d /* %gf */" % (v, f), P_ATOM if v >= 0 else P_UN, DC.T_INT)
        return Ex("%d" % v, P_ATOM if v >= 0 else P_UN, DC.T_INT if t is None else t)

    def operand(self, ins, idx, want=None):
        """Render operand `idx` of instruction `ins` as an expression."""
        o = ins.ops[idx]
        if o.mode in (0, 1):
            return self.const_ex(o.mode, want, o.mode)
        if o.mode == 2:
            return self.const_ex(s16(o.words[0]), want, 2)
        if o.mode in (3, 6):
            slot = s16(o.words[0]) if o.mode == 3 else s32(*o.words)
            nm = ins.name
            w = 4
            if nm in ("MOVV", "ADDV", "SUBV", "DOT", "VTOS", "JEQ_V", "JNE_V", "EQ_V", "NE_V") or \
               (nm in ("MULVS", "DIVVS") and idx == 1):
                w = 12
            if idx == 0 and nm in ("MOVV", "ADDV", "SUBV", "MULVS", "DIVVS"):
                w = 12
            if want is not None and want.kind in ("Vector", "struct"):
                w = max(w, want.size)
            if slot in self.pending:
                pv = self.pending.pop(slot)
                if w == 4 and pv.t is not None and pv.t.kind == "Vector":
                    return Ex("%s.x" % pv.txt(P_ATOM), P_ATOM, DC.T_FLOAT)
                return pv
            if w == 4:
                for b in (4, 8):
                    pv = self.pending.get(slot - b)
                    if pv is not None and pv.t is not None and pv.t.kind == "Vector":
                        self.pending.pop(slot - b)
                        return Ex("%s.%s" % (pv.txt(P_ATOM), "xyz"[b // 4]),
                                  P_ATOM, DC.T_FLOAT)
            return self.var_ref(slot, w)
        if o.mode == 4:
            v = s32(*o.words)
            if o.fix:
                k, sym = o.fix[0]
                if k == "I":
                    return Ex(sym.split()[-1], P_ATOM, self.types.parse(sym.split()[0]))
                if k == "C":
                    try:
                        p = DC.Proto(sym, self.types)
                        ft = self.types.parse("(%s(%s))" % (p.ret.txt, ",".join(x.txt for x in p.params)))
                        return Ex(p.name, P_ATOM, ft)
                    except Exception:
                        return Ex("/* fn %s */ 0" % sym, P_ATOM, None)
                if k == "N":
                    return Ex("/* native %s */ 0" % sym, P_ATOM, None)
            return self.const_ex(v, want, 4)
        if o.mode == 5:
            v = s32(*o.words)
            fixd = [f for f in o.fix if f[0] == "D"]
            fixm = [f for f in o.fix if f[0] == "M"]
            if fixd and fixd[0][1] == self.d.stem_data and not fixm:
                if v in self.d.string_at:
                    return Ex('"%s"' % esc(self.d.string_at[v]), P_ATOM, DC.T_STRING)
                return Ex('/* data[%X] */ ""' % v, P_ATOM, DC.T_STRING)
            if fixd:
                g = self.m.global_type.get(fixd[0][1])
                if g is None:
                    return Ex("/* %s */" % fixd[0][1], P_ATOM, None)
                gt = self.types.parse(g[0])
                base = Ex(ident(g[1]), P_ATOM, gt)
                if fixm:
                    mtxt = fixm[0][1]
                    mt, _, rest = mtxt.partition(" ")
                    mem = rest.split("::")[-1]
                    return Ex("%s.%s" % (base.s, mem), P_ATOM, self.types.parse(mt))
                if gt.kind == "struct" and (v != 0 or ins.name != "LEA"):
                    # A struct global's member: the offset is baked into the operand,
                    # leaving only the D fixup.
                    mem = self.m.member_path(gt.txt, v)
                    if mem is not None and (v != 0 or mem[1].size <= 4):
                        return Ex("%s.%s" % (base.s, mem[0]), P_ATOM, mem[1])
                return base
            if o.fix and o.fix[0][0] == "N":
                return Ex("/* native %s */" % o.fix[0][1], P_ATOM, None)
            return Ex("/* handle %08X */ 0" % (v & 0xFFFFFFFF), P_ATOM, None)
        if o.mode == 7:
            base = s16(o.words[0])
            if base in self.pending:
                b = self.pending[base]
                self._peeked.add(base)
            else:
                b = self.var_ref(base)
            if o.fix16:
                mtxt = o.fix16[0][1]
                mt, _, rest = mtxt.partition(" ")
                mem = rest.split("::")[-1]
                return Ex("%s.%s" % (b.txt(P_ATOM), mem), P_ATOM, self.types.parse(mt))
            off = s16(o.words[1])
            if off:
                cls = None
                bt = b.t if b.t is not None else self.type_of(base)
                if bt is not None:
                    if bt.kind in ("ptr", "obj", "cls") and bt.inner:
                        cls = bt.inner.lstrip("@")
                    elif bt.kind == "struct":
                        cls = bt.txt.lstrip("@")
                mem = self.m.member_path(cls, off) if cls else None
                if mem:
                    return Ex("%s.%s" % (b.txt(P_ATOM), mem[0]), P_ATOM, mem[1])
            if off == 0:
                return Ex("*%s" % b.txt(P_UN), P_UN, None)
            return Ex("*%s /* +%d */" % (b.txt(P_UN), off), P_UN, None)
        return Ex("/* mode%d */" % o.mode)

    def slot_decl_type(self, slot, width=4):
        """The declared type of a stack slot that is a struct member / vector lane."""
        for base, (nm, st) in self.struct_at.items():
            if base <= slot < base + max(4, st.size) and self._struct_live(base):
                mem = self.m.member_path(st.txt, slot - base)
                if mem:
                    return mem[1]
                if slot == base:
                    return st
        for b in (0, 4, 8):
            if (slot - b) in self.vec_base:
                return DC.T_VEC if (b == 0 and width >= 12) else DC.T_FLOAT
        return self.type_of(slot)

    def dest_slot(self, ins):
        o = ins.ops[0]
        return s16(o.words[0]) if o.mode == 3 else None

    # --------------------------------------------------------- linear decode
    def _pending_use(self, slot):
        """Does a later instruction in this block read `slot` before redefining it?"""
        i = getattr(self, "cur", None)
        if i is None:
            return False
        _s0, e0 = self.blocks[self.blk_of[i]]
        for j in range(i + 1, e0):
            if j in self.skip:
                continue
            if slot in self.du[j][1]:
                return True
            if slot in self.du[j][0]:
                return False
        return False

    def _flush(self, out):
        """Materialise pending temporaries that must not cross a statement."""
        for slot in sorted(self.pending):
            if slot in self.block_slots or slot in self._peeked:
                continue
            if self._pending_use(slot):
                continue
            val = self.pending.pop(slot)
            self._emit_store(slot, val, out, 4 if (val.t is None or val.t.kind != "Vector") else 12)

    def _fillers(self, slot, out):
        while self.next_slot < slot:
            sl = self.next_slot
            if sl in self.referenced or sl in self.declared:
                return
            self.declared.add(sl)
            self.next_slot += 4
            out.append(decl_st("int", "_unused%X" % (sl & 0xFFFF), slot=sl))

    def _advance(self, slot, size):
        self.next_slot = max(self.next_slot, slot + max(4, size))

    def _release_dead_struct(self, slot):
        """A struct's base slot stays in `declared` after its DTORPOP; drop the
        stale registration so the slot can be re-declared as a fresh local."""
        if slot in self.struct_at and not self._struct_live(slot):
            del self.struct_at[slot]
            self.struct_at_end.pop(slot, None)
            self.declared.discard(slot)

    def ensure_decl(self, slot, t, out):
        self._web_sync(slot)
        self._release_dead_struct(slot)
        if slot in self.declared or slot < 8:
            return
        self._fillers(slot, out)
        self.declared.add(slot)
        self._advance(slot, t.size)
        out.append(decl_st(t.txt, self.name_for(slot), slot=slot, size=t.size))

    def _emit_store(self, slot, val, out, width=4):
        self._web_sync(slot)
        self._release_dead_struct(slot)
        base = None
        for b in (0, 4, 8):
            if (slot - b) in self.vec_base:
                base = slot - b
                break
        if base is not None and base >= 8 and base not in self.declared and \
                not any(base >= sb and base < sb + max(4, st.size)
                        for sb, (_, st) in self.struct_at.items() if self._struct_live(sb)):
            if width >= 12 and base == slot:
                self._fillers(base, out)
                self.declared.add(base)
                self._advance(base, 12)
                out.append(decl_st("Vector", self.name_for(base), val.txt(0), slot=base, size=12))
                return
            self.ensure_decl(base, DC.T_VEC, out)
        if slot >= 8 and slot not in self.names and base is None and not self._in_struct(slot):
            t = self.type_of(slot) or val.t or DC.T_INT
            if t.kind == "void":
                t = val.t or DC.T_INT
            self.names[slot] = self._mkname(t, "v%X" % (slot & 0xFFFF))
        ref = self.var_ref(slot, width)
        if "." not in ref.s and slot >= 8 and slot not in self.declared and \
                not self._in_struct(slot):
            t = self.type_of(slot) or val.t or DC.T_INT
            if t.kind == "void":
                t = val.t or DC.T_INT
            self._fillers(slot, out)
            self.declared.add(slot)
            self._advance(slot, t.size)
            out.append(decl_st(t.txt, ref.s, val.txt(0), slot=slot, size=t.size))
            return
        out.append(St("raw", "%s = %s;" % (ref.txt(P_ATOM), val.txt(0))))

    def _op_key(self, o):
        """A comparable identity for an operand used as an lvalue."""
        if o.mode == 3:
            return ("s", s16(o.words[0]))
        if o.mode == 5:
            return ("g", tuple(o.words), tuple(o.fix))
        if o.mode == 7:
            return ("m", s16(o.words[0]), s16(o.words[1]), tuple(o.fix16))
        return None

    def _compound(self, ins, op, b, out):
        """`OP d, d, x` renders as `d OP= x` (the lvalue is evaluated once)."""
        k0 = self._op_key(ins.ops[0])
        if k0 is None or k0 != self._op_key(ins.ops[1]):
            return False
        o = ins.ops[0]
        w = 12 if ins.name in ("ADDV", "SUBV", "MULVS", "DIVVS", "MOVV") else 4
        if o.mode == 3:
            slot = s16(o.words[0])
            self._web_sync(slot)
            if self.ret_slot is not None and slot == self.ret_slot:
                return False
            if slot >= 8 and (slot not in self.declared or self.classify(self.cur, slot) == "temp"):
                return False
            self._flush(out)
            dst = self.var_ref(slot, w)
        else:
            self._flush(out)
            dst = self.operand(ins, 0)
        out.append(St("raw", "%s %s= %s;" % (dst.txt(P_ATOM), op, b.txt(0))))
        return True

    def store(self, ins, val, out):
        """Write `val` to instruction `ins`'s destination operand."""
        o = ins.ops[0]
        width = 12 if ins.name in ("MOVV", "ADDV", "SUBV", "MULVS", "DIVVS") else 4
        if o.mode == 3:
            slot = s16(o.words[0])
            self._web_sync(slot)
            if self.ret_slot is not None and slot == self.ret_slot:
                self._flush(out)
                out.append(St("raw", "return %s;" % val.txt(0)))
                self.returned = True
                return
            cls = self.classify(self.cur, slot)
            if cls == "temp":
                self.pending[slot] = val
                return
            if cls == "dead" and ins.name == "STRNEW":
                return
            # This definition kills whatever the slot held; the same slot may be
            # call storage elsewhere in the function.
            for k in range(slot, slot + width, 4):
                self.pending.pop(k, None)
            self._flush(out)
            self._emit_store(slot, val, out, width)
            return
        # global / member / deref destination
        self._flush(out)
        dst = self.operand(ins, 0)
        out.append(St("raw", "%s = %s;" % (dst.txt(P_ATOM), val.txt(0))))

    def names_from_struct(self):
        s = set()
        for base, (nm, st) in self.struct_at.items():
            for k in range(0, max(4, st.size), 4):
                s.add(base + k)
        return s

    def step(self, i, out):
        for sl in self._peeked:
            self.pending.pop(sl, None)
        self._peeked = set()
        return self._step(i, out)

    def _step(self, i, out):
        """Decode one non-jump instruction into `out` / `self.pending`."""
        self.cur = i
        ins = self.ins[i]
        nm = ins.name
        if i in self.skip or nm in ("ENTER", "DTORPOP", "CTOR", "STRFREE"):
            return
        if nm == "RET":
            return
        if nm in ("TRY", "ENDTRY"):
            out.append(St("comment", "%s (no source construct -- see docs/dante_lang.md)" % nm))
            return
        if nm == "LEA":
            if ins.ops[0].mode == 3 and ins.ops[1].mode == 3 and \
               s16(ins.ops[0].words[0]) == s16(ins.ops[1].words[0]) and (i + 1) in self.call:
                p = self.call[i + 1][0]
                if p.name == "constructor" and p.cls:
                    slot = s16(ins.ops[0].words[0])
                    st = self.types.parse(p.cls)
                    nmv = self._mkname(st, "st%X" % slot)
                    self._fillers(slot, out)
                    self._advance(slot, st.size)
                    self.struct_at[slot] = (nmv, st)
                    self.struct_at_end[slot] = self._struct_ctor_end.get(i, 1 << 30)
                    self.skip.add(i + 1)
                    if i + 2 < self.n and self.ins[i + 2].name == "CTOR":
                        self.skip.add(i + 2)
                    out.append(decl_st(st.txt, nmv, slot=slot, size=st.size))
                    self.declared.add(slot)
                    return
            w = self.want_of.get(i)
            if w is not None and w.kind == "ptr" and w.inner:
                w = self.types.parse(w.inner)
            elif w is not None and w.kind in ("obj", "cls"):
                w = self.types.parse(w.txt.lstrip("@"))
            val = self.operand(ins, 1, w)
            val = Ex(val.s, val.p, val.t, lea=True)
            self.store(ins, val, out)
            return
        if nm == "STRNEW":
            slot = self.dest_slot(ins)
            if slot is None:
                return
            nxt = self.ins[i + 1] if i + 1 < self.n else None
            if nxt is not None and nxt.name == "STRCPY" and nxt.ops[0].mode == 3 and \
                    s16(nxt.ops[0].words[0]) == slot:
                return                       # the following STRCPY carries the declaration
            # A String local is released at scope exit.  The slot can look dead here
            # because every path redefines it first.
            _s0, _e0 = self.blocks[self.blk_of[i]]
            scoped = (slot in self.scope_free and slot not in self.block_slots and
                      not any(slot in self.du[k][0] for k in range(i + 1, _e0)))
            if slot < 8 or (self.classify(i, slot) != "local" and not scoped):
                if slot in self.block_slots or slot < 8:
                    self.pending[slot] = Ex('""', P_ATOM, DC.T_STRING)
                return                       # a String temporary inside a statement
            self.pending.pop(slot, None)
            if slot not in self.declared:
                self._fillers(slot, out)
                self.declared.add(slot)
                self._advance(slot, 4)
                self.slot_type.setdefault(slot, DC.T_STRING)
                out.append(decl_st("String", self.name_for(slot), slot=slot))
            return
        if nm == "THREAD":
            self._thread(i, out)
            return
        if i in self.call:
            self._call(i, out)
            return
        if nm in ARITH_OP:
            op, prec, kindt = ARITH_OP[nm]
            want = self.types.parse(kindt) if kindt != "Vector" else DC.T_VEC
            a = self.operand(ins, 1, want)
            b = self.operand(ins, 2, DC.T_FLOAT if nm in ("MULVS", "DIVVS") else want)
            if nm in ("SUBI", "SUBF", "SUBV") and ins.ops[1].mode == 0:
                self.store(ins, Ex("-%s" % b.txt(P_UN), P_UN, b.t or want), out)
                return
            if self._compound(ins, op, b, out):
                return
            self.store(ins, binop(a, op, b, prec, want), out)
            return
        if nm in SET_OP:
            op, kindt = SET_OP[nm]
            want = self.types.parse(kindt)
            a = self.operand(ins, 1, want)
            b = self.operand(ins, 2, want if kindt != "int" else (a.t or want))
            if nm == "EQ_B" and ins.ops[2].mode == 0:
                self.store(ins, Ex("!%s" % a.txt(P_UN), P_UN, DC.T_BOOL), out)
                return
            self.store(ins, binop(a, op, b, P_EQ if op in ("==", "!=") else P_REL, DC.T_BOOL), out)
            return
        if nm == "MOV" and self.ret_slot is not None and ins.ops[0].mode == 3 and \
                s16(ins.ops[0].words[0]) == self.ret_slot and ins.ops[1].mode == 3:
            # A copy into the return slot from an arithmetic temp means the original
            # named a local first; a returned expression goes straight into the slot.
            sslot = s16(ins.ops[1].words[0])
            if sslot >= 8 and sslot in self.pending and sslot not in self.block_slots:
                val = self.pending.pop(sslot)
                self._flush(out)
                self._emit_store(sslot, val, out, 4)
                out.append(St("raw", "return %s;" % self.name_for(sslot)))
                self.returned = True
                return
        if nm in ("MOV", "MOVV", "STRCPY"):
            want = self.want_of.get(i)
            if want is None and ins.ops[0].mode == 3:
                want = self.slot_decl_type(self.dest_slot(ins),
                                           12 if nm == "MOVV" else 4)
            if want is None and ins.ops[0].mode == 5 and ins.ops[0].fix:
                fixm = [f for f in ins.ops[0].fix if f[0] == "M"]
                fixd = [f for f in ins.ops[0].fix if f[0] == "D"]
                if fixm:
                    want = self.types.parse(fixm[0][1].partition(" ")[0])
                elif fixd:
                    g = self.m.global_type.get(fixd[0][1])
                    if g:
                        want = self.types.parse(g[0])
                        if want.kind == "struct":
                            mem = self.m.member_path(want.txt, s32(*ins.ops[0].words))
                            if mem is not None:
                                want = mem[1]
            if want is None and ins.ops[0].mode == 7 and ins.ops[0].fix16:
                want = self.types.parse(ins.ops[0].fix16[0][1].partition(" ")[0])
            if want is None and self.ret_slot is not None and ins.ops[0].mode == 3 and \
                    s16(ins.ops[0].words[0]) == self.ret_slot:
                want = self.proto.ret
            if nm == "MOVV":
                want = DC.T_VEC
            if nm == "STRCPY" and (want is None or want.kind != "String"):
                want = DC.T_STRING
            self.store(ins, self.operand(ins, 1, want), out)
            return
        if nm in ("FTOI", "ITOF", "ITOS", "FTOS", "BTOS", "VTOS"):
            fnname = {"FTOI": "toInt", "ITOF": "toFloat"}.get(nm, "toString")
            src = {"FTOI": DC.T_FLOAT, "ITOF": DC.T_INT, "ITOS": DC.T_INT,
                   "FTOS": DC.T_FLOAT, "BTOS": DC.T_BOOL, "VTOS": DC.T_VEC}[nm]
            a = self.operand(ins, 1, src)
            self.store(ins, Ex("%s(%s)" % (fnname, a.txt(0)), P_ATOM,
                               self.types.parse(DEST_TYPE[nm])), out)
            return
        if nm == "DOT":
            a = self.operand(ins, 1, DC.T_VEC)
            b = self.operand(ins, 2, DC.T_VEC)
            self.store(ins, Ex("dot(%s, %s)" % (a.txt(0), b.txt(0)), P_ATOM, DC.T_FLOAT), out)
            return
        if nm == "DYNCAST":
            cn = DV.class_table().get(ins.ops[2].value() & 0xFFFFFFFF)
            a = self.operand(ins, 1)
            if cn is None:
                raise Bail("unknown DYNCAST class id %d" % ins.ops[2].value())
            t = self.types.parse("@" + cn)
            self.store(ins, Ex("(@%s) %s" % (cn, a.txt(P_UN)), P_UN, t), out)
            return
        raise Bail("unhandled opcode %s at %X" % (nm, ins.off))

    def _is_arg_string(self, i, slot):
        """True when STRNEW at i materialises a String argument temp."""
        k = self.blk_of[i]
        s, e = self.blocks[k]
        for j in range(i + 1, e):
            if j in self.call:
                p, native, base, end, retoff, thisoff, argoffs = self.call[j]
                if base <= slot < end and slot in argoffs:
                    for f in range(j + 1, min(e, j + 8)):
                        fi = self.ins[f]
                        if fi.name == "STRFREE" and fi.ops[0].mode == 3 and \
                           s16(fi.ops[0].words[0]) == slot:
                            return True
                    return False
                return False
            if self.ins[j].name in ("CALL", "THREAD"):
                return False
        return False

    def _call(self, i, out):
        ins = self.ins[i]
        p, native, base, end, retoff, thisoff, argoffs = self.call[i]
        recv = None
        if thisoff is not None:
            recv = self.pending.pop(thisoff) if thisoff in self.pending else self.var_ref(thisoff)
        args = []
        for o, t in zip(argoffs, p.params):
            if o in self.pending:
                args.append(self.pending.pop(o))
            else:
                args.append(self.var_ref(o))
        self._flush(out)
        astr = ", ".join(a.txt(0) for a in args)
        if recv is not None:
            s = "%s.%s(%s)" % (recv.txt(P_ATOM), p.name, astr)
        elif p.cls and native:
            raise Bail("static native method %s" % p.text)
        else:
            s = "%s(%s)" % (p.name, astr)
        if getattr(p, "indirect", False):
            out.append(St("comment", "indirect call through a function-value global"
                                     " -- `.dn` has no syntax for this"))
            self.notes.append("indirect call at %X" % ins.off)
        e = Ex(s, P_ATOM, p.ret)
        if retoff is None:
            out.append(St("raw", s + ";"))
            return
        if self.ret_slot is not None and retoff == self.ret_slot:
            out.append(St("raw", "return %s;" % s))
            self.returned = True
            return
        cls = self.classify(i, retoff)
        if cls == "dead" and p.ret.kind == "Vector":
            # The base slot is never read, but the source may take one lane of the
            # returned Vector, as in `spawn.getPos().y`.
            _s0, e0 = self.blocks[self.blk_of[i]]
            alive = {retoff, retoff + 4, retoff + 8}
            reads = []
            for j in range(i + 1, e0):
                if not alive:
                    break
                dd, uu = self.du[j]
                reads.extend((j, o) for o in sorted(alive & uu))
                alive -= dd
            if len(reads) == 1:
                self.pending[retoff] = e
                return
        if cls == "dead":
            out.append(St("raw", s + ";"))
            return
        if cls == "temp":
            self.pending[retoff] = e
            return
        # a real local receives it
        self.cur = i
        self._emit_store(retoff, e, out, p.ret.size)

    def _thread(self, i, out):
        ins = self.ins[i]
        fx = ins.ops[1].fix
        if not fx or fx[0][0] != "C":
            raise Bail("THREAD without a C fixup")
        p = DC.Proto(fx[0][1], self.types)
        h = s16(ins.ops[0].words[0]) if ins.ops[0].mode == 3 else None
        if h is None:
            raise Bail("THREAD to a non-slot destination")
        stack = h + 4
        args = {}
        j = i + 1
        want = sum(t.size for t in p.params)
        got = 0
        while j < self.n and got < want:
            nx = self.ins[j]
            if nx.name == "STRNEW" and nx.ops[0].mode == 7 and \
                    s16(nx.ops[0].words[0]) == stack:
                self.skip.add(j)             # a String argument is created in place
                j += 1
                continue
            if nx.name not in ("MOV", "MOVV", "STRCPY", "LEA") or nx.ops[0].mode != 7 or \
               s16(nx.ops[0].words[0]) != stack:
                # an argument expression may need a call of its own; let the linear
                # decoder run it (its result lands in `pending`)
                if nx.name in ("TRY", "ENDTRY", "RET", "THREAD") or is_jump(nx):
                    break
                sub = []
                try:
                    self.step(j, sub)
                except Exception:
                    break
                if sub:
                    break
                j += 1
                continue
            off = s16(nx.ops[0].words[1])
            self.cur = j
            cur2, wt = 0, None
            for t in p.params:
                if cur2 == off:
                    wt = t
                    break
                cur2 += t.size
            args[off] = self.operand(nx, 1, wt)
            self.skip.add(j)
            got += 4
            j += 1
        self._flush(out)
        cur = 0
        vals = []
        for t in p.params:
            vals.append(args.get(cur, Ex("/* arg@%d */ 0" % cur)))
            cur += t.size
        expr = Ex("thread %s(%s)" % (p.name, ", ".join(v.txt(0) for v in vals)),
                  P_ATOM, DC.T_INT)
        # `thread f(...)` is also an int-valued expression: the handle is copied out
        # of the THREAD destination (`int h = thread f(x);`, `return thread f(x);`)
        nx = self.ins[j] if j < self.n else None
        if nx is not None and nx.name == "MOV" and nx.ops[0].mode == 3 and \
                nx.ops[1].mode == 3 and s16(nx.ops[1].words[0]) == h and \
                j not in self.skip:
            dst = s16(nx.ops[0].words[0])
            self.cur = j
            if self.ret_slot is not None and dst == self.ret_slot:
                self.skip.add(j)
                out.append(St("raw", "return %s;" % expr.txt(0)))
                self.returned = True
                return
            if dst >= 8 and self.classify(j, dst) == "local":
                self.skip.add(j)
                self._emit_store(dst, expr, out, 4)
                return
        out.append(St("raw", "%s;" % expr.txt(0)))

    # ------------------------------------------------------- condition trees
    def _next_branch(self, i, limit):
        j = i
        while j < limit:
            if j != i and j in self.targets:
                return None
            ins = self.ins[j]
            if ins.name in ("TRY", "ENDTRY"):
                return None
            if is_jump(ins):
                return None if is_goto(ins) else j
            if self._produces_stmt(j):
                return None
            j += 1
        return None

    def _produces_stmt(self, i):
        if i in self.skip:
            return False
        ins = self.ins[i]
        nm = ins.name
        if nm in ("ENTER", "DTORPOP", "CTOR", "STRFREE"):
            return False
        if nm in ("TRY", "ENDTRY", "RET"):
            return True
        if nm == "THREAD":
            return True
        if nm == "LEA" and ins.ops[0].mode == 3 and ins.ops[1].mode == 3 and \
           s16(ins.ops[0].words[0]) == s16(ins.ops[1].words[0]):
            return True
        if nm == "STRNEW":
            slot = self.dest_slot(ins)
            if slot is None:
                return False
            nxt = self.ins[i + 1] if i + 1 < self.n else None
            if nxt is not None and nxt.name == "STRCPY" and nxt.ops[0].mode == 3 and \
                    s16(nxt.ops[0].words[0]) == slot:
                return False
            return slot >= 8 and self.classify(i, slot) == "local"
        if i in self.call:
            p = self.call[i][0]
            retoff = self.call[i][4]
            if retoff is None:
                return True
            return self.classify(i, retoff) != "temp"
        if ins.ops[0].mode != 3:
            return True
        slot = s16(ins.ops[0].words[0])
        if self.ret_slot is not None and slot == self.ret_slot:
            return True
        return self.classify(i, slot) != "temp"

    def _cluster_F(self, i, limit):
        """Guess the `false` target of the condition region starting at i."""
        j = i
        last = None
        while True:
            k = self._next_branch(j, limit)
            if k is None:
                break
            last = k
            j = k + 1
            if j in self.targets:
                break
        return None if last is None else self.tgt[last]

    def condF(self, i, F, limit):
        r = self.unitF(i, F, limit)
        if r is None:
            return None
        node, j = r
        while j < limit and j not in self.targets:
            r2 = self.unitF(j, F, limit)
            if r2 is None:
                break
            node = ("&&", node, r2[0])
            j = r2[1]
        return node, j

    def unitF(self, i, F, limit):
        k = self._next_branch(i, limit)
        if k is None:
            return None
        T = self.tgt[k]
        if T == F:
            return ("leaf", k, True, i), k + 1
        if T > k:
            l = self.condT(i, T, limit)
            if l is None:
                return None
            lnode, j1 = l
            r = self.condF(j1, F, limit)
            if r is None:
                return None
            rnode, j2 = r
            if j2 != T:
                return None
            return ("||", lnode, rnode), j2
        return None

    def condT(self, i, T, limit):
        r = self.unitT(i, T, limit)
        if r is None:
            return None
        node, j = r
        while j < limit and j not in self.targets:
            r2 = self.unitT(j, T, limit)
            if r2 is None:
                break
            node = ("||", node, r2[0])
            j = r2[1]
        return node, j

    def unitT(self, i, T, limit):
        k = self._next_branch(i, limit)
        if k is None:
            return None
        S = self.tgt[k]
        if S == T:
            return ("leaf", k, False, i), k + 1
        if S > k:
            l = self.condF(i, S, limit)
            if l is None:
                return None
            lnode, j1 = l
            r = self.condT(j1, T, limit)
            if r is None:
                return None
            rnode, j2 = r
            if j2 != S:
                return None
            return ("&&", lnode, rnode), j2
        return None

    def build_cond(self, node, out):
        """Walk a condition tree in emit order, rendering the source condition."""
        k = node[0]
        if k == "leaf":
            _, br, negate, start = node
            for j in range(start, br):
                self.step(j, out)
            return self.leaf_expr(br, negate)
        a = self.build_cond(node[1], out)
        b = self.build_cond(node[2], out)
        if k == "&&":
            return binop(a, "&&", b, P_AND, DC.T_BOOL)
        return binop(a, "||", b, P_OR, DC.T_BOOL)

    def leaf_expr(self, br, negate):
        ins = self.ins[br]
        nm = ins.name
        fam, _, suf = nm.partition("_")
        op = (CMP_FALSE if negate else CMP_TRUE)[fam]
        want = self.types.parse(JCMP_TYPE.get(nm, "int"))
        self.cur = br
        a = self.operand(ins, 1, want)
        b = self.operand(ins, 2, a.t or want)
        if suf == "B" and ins.ops[2].mode == 0 and fam in ("JEQ", "JNE"):
            base = a if a.t is None or a.t.kind == "bool" else None
            if base is not None:
                return Ex("!%s" % base.txt(P_UN), P_UN, DC.T_BOOL) if op == "==" else base
        if suf == "I" and ins.ops[2].mode == 0 and fam in ("JEQ", "JNE") and \
           a.t is not None and a.t.kind in ("obj", "ptr", "func", "cls"):
            b = Ex("null", P_ATOM, a.t)
        return binop(a, op, b, P_EQ if op in ("==", "!=") else P_REL, DC.T_BOOL)

    # --------------------------------------------------------- structuring
    def run(self):
        self.returned = False
        out = []
        self.structure(0, self.body_end, [], out)
        self._leftover_decls(out)
        self._split_siblings(out)
        self._hoist(out)
        self._scope_counters(out)
        self.stmts = out
        while self.stmts and self.stmts[-1].kind == "raw" and self.stmts[-1].a == "return;":
            self.stmts.pop()

    def _reused_after(self, slot, iend):
        """Does anything at or past `iend` touch `slot`?  If so it was released there."""
        if iend is None:
            return False
        for i in range(iend, self.n):
            ins = self.ins[i]
            if ins.op == NAMEOP["RET"] or is_goto(ins):
                # End of the fall-through region; past here is sibling code whose slot
                # usage says nothing about this scope.
                return False
            for o in ins.ops:
                if o.mode in (3, 7) and o.nwords and s16(o.words[0]) == slot:
                    return True
        return False

    def _scope_counters(self, stmts):
        """Turn `int n = 0; while (c)` into `for (int n = 0; c; )` when the counter
        dies with the loop, so its slot is released where the shipped code released it."""
        allst = list(walk_stmts(stmts))

        def refs(name):
            rx = re.compile(r"\b%s\b" % re.escape(name))
            return [y for y in allst if rx.search(strip_strings(_st_own_text(y)))]

        def scan(lst):
            for k in range(len(lst) - 1):
                st, w = lst[k], lst[k + 1]
                if st.kind == "raw" and st.decl is not None and st.slot is not None \
                        and st.slot >= 8 and st.decl[2] is not None and \
                        st.decl[0] != "String" and w.kind == "while":
                    name = st.decl[1]
                    inside = set(map(id, walk_stmts([w])))
                    if self._reused_after(st.slot, w.iend) and \
                            all(y is st or id(y) in inside for y in refs(name)):
                        w.kind = "for"
                        w.d = w.b
                        w.b = str(w.a)
                        w.a = "%s %s = %s" % (st.decl[0], name, st.decl[2])
                        w.c = ""
                        w.decl = st.decl
                        w.slot, w.size = st.slot, st.size
                        lst.pop(k)
                        return True
            for st in lst:
                for sub in (st.b, st.c, st.d):
                    if isinstance(sub, list) and scan(sub):
                        return True
            return False

        for _ in range(40):
            if not scan(stmts):
                break
            allst = list(walk_stmts(stmts))

    def _leftover_decls(self, out):
        """Slots that are read but never stored still need a declaration."""
        extra = []
        for slot in sorted(self.names):
            if slot >= 8 and slot not in self.declared and not self._in_struct(slot):
                t = self.type_of(slot) or DC.T_INT
                if t.kind == "void":
                    t = DC.T_INT
                self.declared.add(slot)
                extra.append(decl_st(t.txt, self.names[slot], slot=slot, size=t.size))
        out[0:0] = extra

    # --------------------------------------------------- declaration placement
    def _containers(self, stmts):
        """The scope tree: [statements, parent, anchor, kind, owner] per scope, plus
        the text of the statements that belong to each scope directly."""
        conts = []
        own = []

        def add(lst, parent, anchor, kind, owner):
            conts.append([lst, parent, anchor, kind, owner])
            own.append([])
            return len(conts) - 1

        def walk(cid):
            lst, _p, _a, kind, owner = conts[cid]
            if kind == "hdr":
                own[cid].append(_st_own_text(owner))
                walk(add(owner.d, cid, 0, "blk", None))
                return
            for k, st in enumerate(lst):
                if st.kind == "if":
                    own[cid].append(str(st.a))
                    walk(add(st.b, cid, k, "blk", None))
                    if st.c:
                        walk(add(st.c, cid, k, "blk", None))
                elif st.kind in ("while", "do"):
                    own[cid].append(str(st.a))
                    walk(add(st.b, cid, k, "blk", None))
                elif st.kind == "try":
                    walk(add(st.b, cid, k, "blk", None))
                    walk(add(st.c, cid, k, "blk", None))
                elif st.kind == "for":
                    if st.decl is not None:
                        walk(add(None, cid, k, "hdr", st))
                    else:
                        own[cid].append(_st_own_text(st))
                        walk(add(st.d, cid, k, "blk", None))
                else:
                    own[cid].append(_st_own_text(st))
        walk(add(stmts, -1, 0, "blk", None))
        return conts, [strip_strings(" ".join(t)) for t in own]

    def _split_siblings(self, stmts):
        """One slot reused for two disjoint variables is two locals.  Split them so
        neither declaration gets hoisted into their common parent."""
        for _round in range(6):
            conts, text = self._containers(stmts)

            def anc(cid):
                out = []
                while cid >= 0:
                    out.append(cid)
                    cid = conts[cid][1]
                return out
            if not self._split_once(stmts, conts, text, anc):
                return

    def _split_once(self, stmts, conts, text, anc):
        for cid, c in enumerate(conts):
            if c[3] == "hdr" or c[0] is None:
                continue
            for st in list(c[0]):
                if st.decl is None or st.slot is None:
                    continue
                name = st.decl[1]
                rx = re.compile(r"\b%s\b" % re.escape(name))
                refs = [j for j in range(len(conts)) if rx.search(text[j])]
                if not refs:
                    continue
                common = set(anc(refs[0]))
                for j in refs[1:]:
                    common &= set(anc(j))
                tgt = max(common, key=lambda x: len(anc(x)))
                if conts[tgt][3] == "hdr" or conts[tgt][0] is None:
                    continue
                lst = conts[tgt][0]
                # group tgt's statements into runs that each *start* by defining `name`
                groups = []
                for i, x in enumerate(lst):
                    first = None
                    for y in walk_stmts([x]):
                        if rx.search(strip_strings(_st_own_text(y))):
                            first = y
                            break
                    if first is None:
                        continue
                    t = strip_strings(_st_own_text(first)).strip()
                    isdef = (first.decl is not None and first.decl[1] == name) or \
                        re.match(r"^%s = [^;]*;$" % re.escape(name), t) or \
                        (first.kind == "for" and re.match(r"^%s = " % re.escape(name), t))
                    if isdef:
                        groups.append([i, i, first])
                    elif groups:
                        groups[-1][1] = i
                    else:
                        groups = None
                        break
                if not groups or len(groups) < 2:
                    continue
                # A declaration in `tgt` raises locals_end for every later child scope,
                # so only one group may declare there, and only last.
                own = [n for n, (_g0, _g1, f) in enumerate(groups)
                       if any(f is x for x in lst)]
                if len(own) > 1 or (own and own[0] != len(groups) - 1):
                    continue
                did = False
                for g0, g1, first in groups:
                    if first is st or (first.decl is not None and first.decl[1] == name):
                        continue
                    tname = st.decl[0]
                    new = self._mkname(self.slot_type.get(st.slot),
                                       "v%X" % (st.slot & 0xFFFF))
                    t = _st_own_text(first)
                    if first.kind == "for":
                        head, _, rest = t.partition(";")
                        rhs = head.split("= ", 1)[1] if "= " in head else None
                        first.a = "%s %s = %s" % (tname, new, rhs)
                    else:
                        rhs = t.strip()[len(name) + 3:-1]
                        first.a = "%s %s = %s;" % (tname, new, rhs)
                    first.decl = (tname, new, rhs)
                    first.slot, first.size = st.slot, st.size
                    for i in range(g0, g1 + 1):
                        for y in walk_stmts([lst[i]]):
                            if y is not first:
                                rename_stmt(y, name, new)
                    did = True
                if did:
                    return True
        return False

    def _hoist(self, stmts):
        """Put each declaration in the innermost scope holding all its uses, ordered by
        slot.  This is what makes the recompiled slot numbers line up."""
        conts, text = self._containers(stmts)
        depth = []
        for cid, c in enumerate(conts):
            depth.append(0 if c[1] < 0 else depth[c[1]] + 1)

        def anc(cid):
            out = []
            while cid >= 0:
                out.append(cid)
                cid = conts[cid][1]
            return out

        decls = []          # (cid, index, st, name)
        for cid, (lst, par, anchor, kind, owner) in enumerate(conts):
            if kind == "hdr":
                decls.append((cid, 0, owner, owner.decl[1]))
                continue
            for k, st in enumerate(lst):
                if st.decl is not None and st.kind != "for":
                    decls.append((cid, k, st, st.decl[1]))
        if not decls:
            return

        placed = []         # (target, pos, moved, slot, st, name)
        for cid, k, st, name in decls:
            rx = re.compile(r"\b%s\b" % re.escape(name))
            common = None
            for j in range(len(conts)):
                if not rx.search(text[j]):
                    continue
                ch = set(anc(j))
                common = ch if common is None else (common & ch)
            if not common:
                common = {cid}
            tgt = max(common, key=lambda c: depth[c])
            while conts[tgt][3] == "hdr" and conts[tgt][4] is not st:
                tgt = conts[tgt][1]
            if tgt == cid:
                pos, moved = k, False
            else:
                c = cid
                while conts[c][1] != tgt:
                    c = conts[c][1]
                pos, moved = conts[c][2], True
            placed.append([tgt, pos, moved, st.slot if st.slot is not None else 0, st, name])

        # A child scope's locals start above its ancestors', so a local sitting below
        # one an ancestor declares belongs in that ancestor, ahead of it.
        for _round in range(8):
            moved_any = False
            for a in placed:
                for b in placed:
                    if a is b or b[3] <= a[3] or b[0] == a[0]:
                        continue
                    if b[0] not in anc(a[0]) or conts[b[0]][3] == "hdr":
                        continue
                    c = a[0]
                    while conts[c][1] != b[0]:
                        c = conts[c][1]
                    a[0] = b[0]
                    a[1] = conts[c][2]
                    a[2] = True
                    moved_any = True
                    break
            if not moved_any:
                break

        bytarget = collections.defaultdict(list)
        for it in placed:
            bytarget[it[0]].append(it)
        inserts = collections.defaultdict(lambda: collections.defaultdict(list))
        for tgt, items in bytarget.items():
            items.sort(key=lambda it: it[3])
            # Declarations must run in ascending slot order.  Split off a bare one only
            # where that's violated, and no earlier than it has to go.
            need = [False] * len(items)
            lim = 1 << 30
            for i in range(len(items) - 1, -1, -1):
                tg, pos, moved, slot, st, name = items[i]
                if moved or pos > lim:
                    need[i] = True
                    items[i][1] = pos = min(pos, lim)
                lim = min(lim, pos)
            for i, (tg, pos, moved, slot, st, name) in enumerate(items):
                if not need[i]:
                    continue                       # stays where the walker put it
                tname, _nm, rhs = st.decl
                d = decl_st(tname, name, None, slot=slot, size=st.size)
                inserts[tgt][pos].append((slot, d))
                if st.kind == "for":
                    st.a = "" if rhs is None else "%s = %s" % (name, rhs)
                elif rhs is None:
                    st.kind = "drop"
                else:
                    st.kind = "raw"
                    st.a = "%s = %s;" % (name, rhs)
                st.decl = None
        for tgt, at in inserts.items():
            lst = conts[tgt][0]
            new = []
            for idx in range(len(lst) + 1):
                for _sl, d in sorted(at.get(idx, []), key=lambda x: x[0]):
                    new.append(d)
                if idx < len(lst):
                    new.append(lst[idx])
            lst[:] = new
        for c in conts:
            if c[0] is not None:
                c[0][:] = [st for st in c[0] if st.kind != "drop"]
        self._fill_gaps(stmts, 8)

    def _fill_gaps(self, lst, end):
        """Insert `int _unusedNN;` where the shipped frame has a slot we did not
        recognise, so the locals after it still land on their real offsets."""
        out = []
        for st in lst:
            if st.decl is not None and st.slot is not None and st.kind != "for":
                while end < st.slot:
                    if end in self.referenced:
                        break
                    out.append(decl_st("int", "_unused%X" % (end & 0xFFFF), slot=end))
                    end += 4
                end = max(end, st.slot + st.size)
                out.append(st)
                continue
            if st.kind == "if":
                self._fill_gaps(st.b, end)
                if st.c:
                    self._fill_gaps(st.c, end)
            elif st.kind in ("while", "do"):
                self._fill_gaps(st.b, end)
            elif st.kind == "try":
                self._fill_gaps(st.b, end)
                self._fill_gaps(st.c, end)
            elif st.kind == "for":
                if st.decl is not None and st.slot is not None:
                    while end < st.slot:
                        if end in self.referenced:
                            break
                        out.append(decl_st("int", "_unused%X" % (end & 0xFFFF), slot=end))
                        end += 4
                    self._fill_gaps(st.d, max(end, st.slot + st.size))
                    end = max(end, st.slot + st.size)
                else:
                    self._fill_gaps(st.d, end)
            out.append(st)
        lst[:] = out

    def structure(self, i0, i1, loops, out):
        i = i0
        while i < i1:
            if i in self.targets:
                self.pending.clear()
            ins = self.ins[i]
            if ins.name == "TRY":
                i = self._try(i, i1, loops, out)
                continue
            # -------- loop?
            if is_goto(ins) and self.tgt[i] > i:
                L = self._latch(i + 1, i1)
                if L is not None and i + 1 <= self.tgt[i] <= L:
                    i = self._loop(i, L, i1, loops, out)
                    continue
            back = self._backlatch(i, i1)
            if back is not None:
                i = self._bareloop(i, back, i1, loops, out)
                continue
            if not is_jump(ins):
                if i in self.skip:
                    i += 1
                    continue
                self.step(i, out)
                if getattr(self, "returned", False):
                    self.returned = False
                    if i + 1 < i1 and is_goto(self.ins[i + 1]) and self.tgt[i + 1] == self.epi:
                        i += 1
                i += 1
                continue
            # -------- unconditional jump
            if is_goto(ins):
                T = self.tgt[i]
                if T == self.epi:
                    out.append(St("raw", "return;"))
                    i += 1
                    continue
                st = self._ctl_stmt(T, loops)
                if st is not None:
                    out.append(st)
                    i += 1
                    continue
                if self._same_exit(T, i1):
                    i += 1
                    continue
                raise Bail("unstructured goto at %X -> %X" % (ins.off, self.ins[T].off))
            # -------- conditional -> if / else
            i = self._if(i, i1, loops, out)
        return

    def _try(self, i, i1, loops, out):
        """`TRY -> H; body; ENDTRY; goto E; H: handler; E:`  ->  try/catch."""
        H = self.tgt[i]
        depth = 0
        e = None
        for j in range(i + 1, i1):
            if self.ins[j].name == "TRY":
                depth += 1
            elif self.ins[j].name == "ENDTRY":
                if depth == 0:
                    e = j
                    break
                depth -= 1
        if e is None or not (i < e < H <= i1):
            raise Bail("unmatched TRY at %X" % self.ins[i].off)
        body_end = e
        if e + 1 < i1 and is_goto(self.ins[e + 1]):
            E = self.tgt[e + 1]
            if self._same_exit(E, i1):
                E = i1
            if H != e + 2 or not (H <= E <= i1):
                raise Bail("TRY region at %X" % self.ins[i].off)
        elif H == e + 1:
            E = i1                       # the body falls straight into the handler
        else:
            raise Bail("TRY region at %X" % self.ins[i].off)
        self.pending.clear()
        body = []
        self.structure(i + 1, body_end, loops, body)
        self.pending.clear()
        handler = []
        self.structure(H, E, loops, handler)
        self.pending.clear()
        st = St("try", "", body, handler)
        out.append(st)
        return E

    def _backlatch(self, i, i1):
        """The last jump in [i, i1) that targets i (a loop whose test is at the end)."""
        best = None
        for j in range(i, i1):
            if is_jump(self.ins[j]) and self.tgt[j] == i:
                best = j
        return best

    def _bareloop(self, head, L, i1, loops, out):
        end = L + 1
        body = []
        self.pending.clear()
        self.structure(head, L, loops + [(L, end)], body)
        # the body's trailing temporaries feed the latch condition -- do not drop them
        if is_goto(self.ins[L]):
            self.pending.clear()
            out.append(St("for", "", "", "", body))
        else:
            node = self.condT(L, head, L + 1)
            if node is None or node[1] != L + 1:
                raise Bail("do/while condition at %X" % self.ins[L].off)
            setup = []
            cond = self.build_cond(node[0], setup)
            if setup:
                raise Bail("do/while condition emits statements at %X" % self.ins[L].off)
            out.append(St("do", cond.txt(0), body))
        self.pending.clear()
        return end

    def _latch(self, lo, hi):
        best = None
        for j in range(lo, hi):
            if is_jump(self.ins[j]) and self.tgt[j] == lo:
                best = j
        return best

    def _loop(self, gi, L, i1, loops, out):
        cond_start = self.tgt[gi]
        head = gi + 1
        end = L + 1
        # `continue` targets inside the body region
        conts = sorted({self.tgt[j] for j in range(head, cond_start)
                        if is_jump(self.ins[j]) and head < self.tgt[j] < cond_start})
        step_start = None
        for c in conts:
            step_start = c
            break
        body_out = []
        self.pending.clear()
        ok = False
        for attempt in (0, 1):
            body_out = []
            self.pending.clear()
            try:
                if attempt == 0:
                    self.structure(head, cond_start, loops + [(cond_start, end)], body_out)
                else:
                    if step_start is None:
                        raise Bail("no step candidate")
                    self.structure(head, step_start, loops + [(step_start, end)], body_out)
                ok = True
                break
            except Bail:
                continue
        if not ok:
            raise Bail("loop body at %X" % self.ins[head].off)
        self.pending.clear()
        # condition
        cond_txt = "true"
        if cond_start < L or not is_goto(self.ins[L]):
            setup = []
            node = self.condT(cond_start, head, L + 1)
            if node is None or node[1] != L + 1:
                raise Bail("loop condition at %X" % self.ins[cond_start].off)
            cond = self.build_cond(node[0], setup)
            if setup:
                raise Bail("loop condition emits statements")
            cond_txt = cond.txt(0)
        step_txt = None
        if attempt == 1:
            steps = []
            self.pending.clear()
            self.structure(step_start, cond_start, loops, steps)
            if len(steps) == 1 and steps[0].kind == "raw":
                step_txt = steps[0].a.rstrip(";")
            else:
                body_out.extend(steps)
        # try to lift a trailing `i = i + 1` into a for-step
        init_txt = None
        init_decl = None
        initst = None
        if step_txt is None and body_out and body_out[-1].kind == "raw" and \
                re.match(r"^[A-Za-z_][A-Za-z0-9_.]*\s=\s.*;$", body_out[-1].a) and \
                cond_txt != "true":
            cand = body_out[-1].a.rstrip(";")
            var = cand.split(" =")[0]
            if re.search(r"\b%s\b" % re.escape(var), cond_txt) and re.search(r"\b%s\b" % re.escape(var), cand.split("= ", 1)[-1]):
                nocont = not any(s.kind == "raw" and s.a == "continue;" for s in body_out)
                if nocont:
                    step_txt = cand
                    body_out.pop()
                    if out and out[-1].kind == "raw" and out[-1].a.rstrip(";").split(" =")[0].split()[-1] == var:
                        initst = out.pop()
                        init_txt = initst.a.rstrip(";")
                        init_decl = initst.decl
        if step_txt is not None:
            fst = St("for", init_txt or "", cond_txt, step_txt, body_out)
            fst.decl = init_decl
            if initst is not None:
                fst.slot, fst.size = initst.slot, initst.size
        elif cond_txt == "true":
            fst = St("for", "", "", "", body_out)
        else:
            fst = St("while", cond_txt, body_out)
        fst.iend = end
        out.append(fst)
        self.pending.clear()
        return end

    def run_goto(self):
        """Unstructured rendering: labels + `goto`, used where the graph defeats
        the structurer.  Compiles: `.dn` has `IDENT:` labels and `goto IDENT;`."""
        self._reset_state()
        out = []
        used = set()
        for i in range(self.n):
            ins = self.ins[i]
            if is_jump(ins) and self.tgt[i] != self.epi:
                used.add(self.tgt[i])
        for i in range(self.body_end):
            ins = self.ins[i]
            if i in used:
                self._flush(out)
                self.pending.clear()
                out.append(St("label", "L_%X" % ins.off))
            try:
                if ins.name == "TRY":
                    self._flush(out)
                    out.append(St("_try", "L_%X" % self.ins[self.tgt[i]].off))
                elif ins.name == "ENDTRY":
                    self._flush(out)
                    out.append(St("_endtry"))
                elif is_goto(ins):
                    T = self.tgt[i]
                    out.append(St("raw", "return;" if T == self.epi
                                  else "goto L_%X;" % self.ins[T].off))
                elif is_jump(ins):
                    self.cur = i
                    cond = self.leaf_expr(i, False)
                    T = self.tgt[i]
                    self._flush(out)
                    out.append(St("raw", "if (%s) %s" % (
                        cond.txt(0), "return;" if T == self.epi
                        else "goto L_%X;" % self.ins[T].off)))
                else:
                    self.step(i, out)
            except Exception as ex:
                self.pending.clear()
                out.append(St("comment", "!! %s   [%5X: %s]" % (ex, ins.off, self.d.fmt_instr(ins))))
        # an exception scope is never silently dropped: either it folds back into
        # `try { } catch { }` or the body is marked unsupported for the caller
        self.goto_unsupported = not self._try_wrap(out)
        self._leftover_decls(out)
        self._hoist(out)
        self.stmts = out
        self.used_goto = True

    def _try_wrap(self, out):
        """Fold a goto body's TRY/ENDTRY markers back into try/catch.

        False if some TRY doesn't fit the shape, meaning the body isn't compilable."""
        while True:
            p = next((i for i, st in enumerate(out) if st.kind == "_try"), None)
            if p is None:
                return True
            q = next((i for i in range(p + 1, len(out)) if out[i].kind == "_endtry"), None)
            if q is None:
                return False
            if any(out[i].kind == "_try" for i in range(p + 1, q)):
                return False                     # nested TRY: not modelled
            catch_name = out[p].a
            # ENDTRY must be followed by the `goto end` that skips the handler
            if q + 1 >= len(out) or out[q + 1].kind != "raw" or \
                    not re.match(r"^goto [A-Za-z_][A-Za-z0-9_]*;$", out[q + 1].a or ""):
                return False
            end_name = out[q + 1].a[5:-1]
            if out[q + 2:q + 3] and out[q + 2].kind == "label" and out[q + 2].a == catch_name:
                c = q + 2
            else:
                return False                     # the handler must start right there
            e = next((i for i in range(c + 1, len(out))
                      if out[i].kind == "label" and out[i].a == end_name), None)
            if e is None:
                return False
            # nothing else may jump to the catch label -- it disappears
            if any(st.kind in ("raw", "if", "while", "do") and
                   re.search(r"\bgoto %s\b" % re.escape(catch_name), str(st.a or ""))
                   for st in walk_stmts(out)):
                return False
            st = St("try", None, out[p + 1:q], out[c + 1:e])
            out[p:e] = [st]

    def _reset_state(self):
        self.pending = {}
        self.names = {}
        self.declared = set()
        self.struct_at = {}
        self.struct_at_end = {}
        self.skip = set()
        self.used_names = set()
        self.autodecl = []
        self.next_slot = 8
        self._peeked = set()
        self._webcur = {}
        self._webnames = {}
        self._webdecl = {}
        for slot, groups in self.retype.items():
            if groups[0][1] is not None:
                self.slot_type[slot] = groups[0][1]
            else:
                self.slot_type.pop(slot, None)
        for i, t in enumerate(self.proto.params):
            self.names[self.param_slots[i]] = self.param_decl[i][1]
            self.used_names.add(self.param_decl[i][1])

    # ---- speculative state (candidate `false` targets are tried in turn)
    def _snap(self):
        return (dict(self.pending), dict(self.names), set(self.declared),
                dict(self.struct_at), list(self.autodecl), self.next_slot,
                set(self.skip), set(self.used_names), list(self.notes),
                dict(self.slot_type), set(self.vec_base), set(self._peeked),
                dict(self._webcur), dict(self._webnames), dict(self._webdecl))

    def _restore(self, sn):
        (self.pending, self.names, self.declared, self.struct_at, self.autodecl,
         self.next_slot, self.skip, self.used_names, self.notes, self.slot_type,
         self.vec_base, self._peeked) = (dict(sn[0]), dict(sn[1]), set(sn[2]),
                                         dict(sn[3]), list(sn[4]), sn[5], set(sn[6]),
                                         set(sn[7]), list(sn[8]), dict(sn[9]),
                                         set(sn[10]), set(sn[11]))
        self._webcur = dict(sn[12])
        self._webnames = dict(sn[13])
        self._webdecl = dict(sn[14])

    def _cluster_targets(self, i, limit):
        """Candidate `false` targets for the condition region starting at i."""
        out = []
        j = i
        while len(out) < 5:
            k = self._next_branch(j, limit)
            if k is None:
                break
            t = self.tgt[k]
            if t not in out:
                out.append(t)
            j = k + 1
            if j in self.targets:
                break
        return out

    def _chase(self, T):
        """T plus every index reachable from it through unconditional gotos."""
        out = [T]
        j = T
        for _ in range(8):
            if j is None or j >= self.n or not is_goto(self.ins[j]):
                break
            j = self.tgt[j]
            if j in out:
                break
            out.append(j)
        return out

    def _same_exit(self, T, i1):
        """True when jumping to T is the same as falling out of the region at i1."""
        return i1 in self._chase(T) or T in self._chase(i1)

    def _ctl_stmt(self, T, loops):
        chain = self._chase(T)
        if self.epi is not None and self.epi in chain:
            return St("raw", "return;")
        for cont, brk in reversed(loops):
            if brk in chain:
                return St("raw", "break;")
            if cont in chain:
                return St("raw", "continue;")
        return None

    def _min_temp(self, a, b):
        """The lowest scratch slot the region [a, b) writes (temps + call blocks)."""
        lo = None
        for k in range(a, b):
            ins = self.ins[k]
            if k in self.call:
                base = self.call[k][2]
                if base >= 8:
                    lo = base if lo is None else min(lo, base)
            if ins.ops[0].mode == 3 and OPS[ins.op][1] == "d":
                v = s16(ins.ops[0].words[0])
                if v >= 8:
                    lo = v if lo is None else min(lo, v)
        return lo

    def _ternary(self, i, i1, loops, F, j, cond):
        """Recognise `dest = c ? a : b`.  The giveaway is that it's all one statement:
        the second arm's temporaries stack above the condition's."""
        if not (j <= F - 1 < i1) or not is_goto(self.ins[F - 1]):
            return None
        E = self.tgt[F - 1]
        if self._same_exit(E, i1):
            E = i1
        if not (F <= E <= i1) or F - 1 <= j:
            return None
        floor_ = self.next_slot
        emin = self._min_temp(F, E)
        if emin is None or emin <= floor_:
            return None
        amin = self._min_temp(j, F - 1)
        if amin is not None and amin <= floor_:
            return None
        sn = self._snap()
        try:
            a_out = []
            self.structure(j, F - 1, loops, a_out)
            self.pending.clear()
            b_out = []
            self.structure(F, E, loops, b_out)
            self.pending.clear()
        except Exception:
            self._restore(sn)
            return None
        if len(a_out) != 1 or len(b_out) != 1 or \
                a_out[0].kind != "raw" or b_out[0].kind != "raw":
            self._restore(sn)
            return None
        A, B = a_out[0], b_out[0]
        pick = "%s ? %s : %s" % (cond.txt(P_EQ), "%s", "%s")
        if A.a.startswith("return ") and B.a.startswith("return "):
            st = St("raw", "return %s;" % (pick % (A.a[7:-1], B.a[7:-1])))
            self.returned = True
            return st, E
        if A.decl is not None and B.decl is None and "=" in B.a and \
                B.a.split(" = ", 1)[0] == A.decl[1]:
            rhs = pick % (A.decl[2], B.a.split(" = ", 1)[1][:-1])
            A.a = "%s %s = %s;" % (A.decl[0], A.decl[1], rhs)
            A.decl = (A.decl[0], A.decl[1], rhs)
            return A, E
        if A.decl is None and B.decl is None and " = " in A.a and " = " in B.a and \
                A.a.split(" = ", 1)[0] == B.a.split(" = ", 1)[0]:
            A.a = "%s = %s;" % (A.a.split(" = ", 1)[0],
                                pick % (A.a.split(" = ", 1)[1][:-1],
                                        B.a.split(" = ", 1)[1][:-1]))
            return A, E
        self._restore(sn)
        return None

    def _cond_len(self, i, F, i1):
        """How far the condition region starting at i reaches for this false target."""
        try:
            if (F > i1 or F < i) and not self._same_exit(F, i1):
                r = self.condT(i, F, i1 + 1)
            else:
                r = self.condF(i, F, i1 + 1)
        except Exception:
            return -1
        return r[1] if r else -1

    def _if(self, i, i1, loops, out):
        cands = self._cluster_targets(i, i1)
        if not cands:
            raise Bail("cannot bound the condition at %X" % self.ins[i].off)
        # the shipped compiler folds every branch of one `&&`/`||` chain into a
        # single condition, so prefer the target that consumes the most branches
        cands = [F for _s, _n, F in sorted(
            (-self._cond_len(i, F, i1), n, F) for n, F in enumerate(cands))]
        last = None
        for F in cands:
            sn = self._snap()
            trial = []
            try:
                end = self._if_with(i, i1, loops, trial, F)
            except Bail as ex:
                self._restore(sn)
                last = last or ex
                continue
            out.extend(trial)
            return end
        raise last or Bail("cannot parse the condition at %X" % self.ins[i].off)

    def _if_with(self, i, i1, loops, out, F):
        # a conditional exit from the region: `if (c) break/continue/return;`
        if (F > i1 or F < i) and not self._same_exit(F, i1):
            ctl = self._ctl_stmt(F, loops)
            if ctl is None:
                raise Bail("if region out of range at %X" % self.ins[i].off)
            r = self.condT(i, F, i1 + 1)
            if r is None:
                raise Bail("cannot parse the exit condition at %X" % self.ins[i].off)
            node, j = r
            setup = []
            cond = self.build_cond(node, setup)
            if setup:
                raise Bail("condition emits statements at %X" % self.ins[i].off)
            self.pending.clear()
            out.append(St("if", cond.txt(0), [ctl], None))
            self.notes.append("conditional exit at %X" % self.ins[i].off)
            return j
        r = self.condF(i, F, i1 + 1)
        if r is None:
            raise Bail("cannot parse the condition at %X" % self.ins[i].off)
        node, j = r
        if not (j <= F <= i1) and self._same_exit(F, i1):
            F = i1
        if not (j <= F <= i1):
            raise Bail("if region out of range at %X" % self.ins[i].off)
        setup = []
        cond = self.build_cond(node, setup)
        if setup:
            out.extend(setup)
        self.pending.clear()
        t = self._ternary(i, i1, loops, F, j, cond)
        if t is not None:
            out.append(t[0])
            return t[1]
        then_out = []
        if F - 1 >= j and is_goto(self.ins[F - 1]):
            E = self.tgt[F - 1]
            is_ctl = (E == self.epi) or any(E in lp for lp in loops)
            if not is_ctl and F < E <= i1:
                self.structure(j, F - 1, loops, then_out)
                self.pending.clear()
                els_out = []
                self.structure(F, E, loops, els_out)
                self.pending.clear()
                out.append(St("if", cond.txt(0), then_out, els_out))
                return E
        self.structure(j, F, loops, then_out)
        self.pending.clear()
        out.append(St("if", cond.txt(0), then_out, None))
        return F


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")


# ==========================================================================
def decompile_file(path, extra_symbols=()):
    m = Module(path, extra_symbols)
    m.decompile()
    return m


_ERRLINE = re.compile(r"line (\d+):")


def compilable_source(m, max_rounds=400):
    """Source the compiler accepts, stubbing rejected bodies one at a time.

    Returns (source, stubbed prototypes)."""
    stub = {r.proto for r in m.results if not r.ok or r.goto_unsupported}
    src = m.source(compilable=True, stub=stub)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, (m.stem or "mod") + ".dn")
        for _ in range(max_rounds):
            src = m.source(compilable=True, stub=stub)
            with open(path, "w", newline="\n") as fh:
                fh.write(src)
            try:
                DC.compile_source(path, module=m.stem)
                return src, stub
            except Exception as ex:
                mm = _ERRLINE.search(str(ex))
                victim = None
                if mm:
                    ln = int(mm.group(1))
                    for proto, (a, b) in m.line_of.items():
                        if a <= ln <= b and proto not in stub:
                            victim = proto
                            break
                if victim is None:
                    return src, stub          # not attributable: leave it to the caller
                stub.add(victim)
    return src, stub


def stats(m):
    """(exports, fully structured, labelled-goto fallback, raw fallback)"""
    tot = len(m.results)
    goto = sum(1 for r in m.results if r.ok and r.goto_fallback)
    ok = sum(1 for r in m.results if r.ok and not r.goto_fallback)
    return tot, ok, goto, tot - ok - goto


def cmd_decompile(args):
    files = DV.iter_files(args.target)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
    total = okc = gotoc = rawc = 0
    for f in files:
        m = decompile_file(f, args.symbols)
        t, o, g, rw = stats(m)
        total += t
        okc += o
        gotoc += g
        rawc += rw
        if args.func:
            for r in m.results:
                if args.func in r.proto:
                    print("// @%X  %s" % (r.off, r.proto))
                    if not r.ok:
                        print("// FAILED: %s" % r.error)
                    print(r.signature(m))
                    print("{")
                    print("\n".join(render(r.stmts, 1)))
                    print("}")
            continue
        if args.compilable:
            src, stub = compilable_source(m)
            if stub:
                print("// %s: %d body(s) stubbed -- the compiler rejects them" % (
                    os.path.basename(f), len(stub)), file=sys.stderr)
        else:
            src = m.source()
        if args.stdout:
            print(src)
        elif args.out:
            p = os.path.join(args.out, os.path.splitext(os.path.basename(f))[0] + ".dn")
            with open(p, "w", newline="\n") as fh:
                fh.write(src)
            print("%-24s %4d exports  %4d structured  %3d goto  %2d raw -> %s" % (
                os.path.basename(f), t, o, g, rw, p))
        else:
            print("%-24s %4d exports  %4d structured  %3d goto  %2d raw" % (
                os.path.basename(f), t, o, g, rw))
    if len(files) > 1:
        print("total: %d exports, %d structured (%.1f%%), %d labelled-goto, %d raw" % (
            total, okc, 100.0 * okc / max(1, total), gotoc, rawc))
    return 0


def register(sub):
    """Mount `dante decompile` on the CLI."""
    a = sub.add_parser("decompile", help="compiled .dante -> readable .dn source")
    a.add_argument("target", help="a .dante file or a directory of them")
    a.add_argument("-o", "--out", help="output directory for the .dn dumps")
    a.add_argument("--stdout", action="store_true", help="print instead of writing files")
    a.add_argument("--func", help="only show this export (implies --stdout)")
    a.add_argument("--symbols", action="append", default=[])
    a.add_argument("--compilable", action="store_true",
                   help="emit source `dante compile` accepts (only bodies the decompiler could "
                        "not structure, or whose exception scope will not fold back, are stubbed)")
    a.set_defaults(fn=cmd_decompile)
