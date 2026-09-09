"""Parse, disassemble, assemble, verify, compare and link `.dante` modules.

No code address is stored absolutely, which is what lets emit() and link() work."""
import os
import re
import sys
import glob
import struct
import collections

from . import symbols

# ----------------------------------------------------------------------------
# Indexed by opcode: (name, dest, src1, src2).  Roles are d=written, r=read,
# j=jump offset, f=function value, n=frame offset, a=address-of, -=unused.
OPS = [
    ("RET",      "-", "-", "-"),   # 0  pop frame; negative saved PC ends the thread
    ("ENTER",    "n", "-", "-"),   # 1  prologue: check fp+n*2 < stack limit
    ("BEGINTHREAD", "-", "-", "-"),  # 2  never appears in the corpus; semantics unknown
    ("DTORPOP",  "r", "-", "-"),   # 3  pop destructor stack (must be dest), run dtor
    ("STRFREE",  "r", "-", "-"),   # 4  String destructor on dest
    ("TRY",      "j", "-", "-"),   # 5  push catch frame (PC+dest, fp) on dtor stack
    ("MOV",      "d", "r", "-"),   # 6  8-byte copy
    ("MOVV",     "d", "r", "-"),   # 7  24-byte copy (Vector3 = 3 slots)
    ("STRCPY",   "d", "r", "-"),   # 8  String assign
    ("FTOI",     "d", "r", "-"),   # 9  floor
    ("ITOF",     "d", "r", "-"),   # 10
    ("ITOS",     "d", "r", "-"),   # 11 "%d"
    ("FTOS",     "d", "r", "-"),   # 12 "%g"
    ("BTOS",     "d", "r", "-"),   # 13 "true"/"false"
    ("VTOS",     "d", "r", "-"),   # 14 "{%g, %g, %g}"
    ("LEA",      "d", "a", "-"),   # 15 dest = address of src1
    ("ENDTRY",   "-", "-", "-"),   # 16 pop the TRY frame (operand ignored)
    ("ADDI",     "d", "r", "r"),   # 17
    ("ADDF",     "d", "r", "r"),   # 18
    ("ADDV",     "d", "r", "r"),   # 19
    ("STRCAT",   "d", "r", "r"),   # 20
    ("SUBI",     "d", "r", "r"),   # 21
    ("SUBF",     "d", "r", "r"),   # 22
    ("SUBV",     "d", "r", "r"),   # 23
    ("MULI",     "d", "r", "r"),   # 24
    ("MULF",     "d", "r", "r"),   # 25
    ("MULVS",    "d", "r", "r"),   # 26 vector * scalar
    ("DOT",      "d", "r", "r"),   # 27
    ("DIVI",     "d", "r", "r"),   # 28
    ("DIVF",     "d", "r", "r"),   # 29
    ("DIVVS",    "d", "r", "r"),   # 30 vector / scalar
    ("MODI",     "d", "r", "r"),   # 31 (result always >= 0)
    ("MODF",     "d", "r", "r"),   # 32
    ("JLT_I",    "j", "r", "r"),   # 33 if src1 <  src2 goto PC+dest
    ("JLT_F",    "j", "r", "r"),   # 34
    ("JLE_I",    "j", "r", "r"),   # 35
    ("JLE_F",    "j", "r", "r"),   # 36
    ("JEQ_I",    "j", "r", "r"),   # 37
    ("JEQ_F",    "j", "r", "r"),   # 38
    ("JEQ_V",    "j", "r", "r"),   # 39
    ("JEQ_S",    "j", "r", "r"),   # 40
    ("JEQ_B",    "j", "r", "r"),   # 41
    ("JNE_I",    "j", "r", "r"),   # 42
    ("JNE_F",    "j", "r", "r"),   # 43
    ("JNE_V",    "j", "r", "r"),   # 44
    ("JNE_S",    "j", "r", "r"),   # 45
    ("JNE_B",    "j", "r", "r"),   # 46
    ("LT_I",     "d", "r", "r"),   # 47 dest = src1 < src2
    ("LT_F",     "d", "r", "r"),   # 48
    ("LE_I",     "d", "r", "r"),   # 49
    ("LE_F",     "d", "r", "r"),   # 50
    ("EQ_I",     "d", "r", "r"),   # 51
    ("EQ_F",     "d", "r", "r"),   # 52
    ("EQ_V",     "d", "r", "r"),   # 53
    ("EQ_S",     "d", "r", "r"),   # 54
    ("EQ_B",     "d", "r", "r"),   # 55
    ("NE_I",     "d", "r", "r"),   # 56
    ("NE_F",     "d", "r", "r"),   # 57
    ("NE_V",     "d", "r", "r"),   # 58
    ("NE_S",     "d", "r", "r"),   # 59
    ("NE_B",     "d", "r", "r"),   # 60
    ("CALL",     "f", "n", "n"),   # 61 VM: frame at fp+src1; native: args at fp+src2 (-1 none)
    ("CTOR",     "d", "r", "f"),   # 62 register dtor(src2) for object dest (src1 = thread, 0 = self)
    ("STRNEW",   "d", "r", "-"),   # 63 dest = "" + register String dtor (src1 = thread)
    ("DYNCAST",  "d", "r", "r"),   # 64 dest = isa(src1, classId src2) ? src1 : null
    ("THREAD",   "d", "f", "n"),   # 65 dest = {id, stack}; new thread at fn src1, frame src2
]
OPNAME = {i: o[0] for i, o in enumerate(OPS)}
NAMEOP = {o[0]: i for i, o in enumerate(OPS)}
MODE_WORDS = {0: 0, 1: 0, 2: 1, 3: 1, 4: 2, 5: 2, 6: 2, 7: 2}


def s16(w):
    return w - 0x10000 if w & 0x8000 else w


def s32(hi, lo):
    v = ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
    return v - 0x100000000 if v & 0x80000000 else v


def w32(v):
    v &= 0xFFFFFFFF
    return ((v >> 16) & 0xFFFF, v & 0xFFFF)


def fbits(f):
    return struct.unpack("<I", struct.pack("<f", f))[0]


def bitsf(v):
    return struct.unpack("<f", struct.pack("<I", v & 0xFFFFFFFF))[0]


# ----------------------------------------------------------------------------
# DYNCAST class ids: the engine's own hash of the class name, case-folded, with
# only letters and digits participating.
def classid(name):
    h = 0
    for ch in name:
        if ch.isalnum():
            h = (h * 0x80 + ord(ch.lower()) * 0x20001 + (h >> 25)) & 0xFFFFFFFF
    return h


def classid_signed(name):
    v = classid(name)
    return v - 0x100000000 if v & 0x80000000 else v


# Names that appear as @CClass in the corpus but are not in dante_api.json's
# "classes" table; kept so DYNCAST immediates still annotate.
EXTRA_CLASSES = ["CWayPoint", "CEctoCarEffects", "CPKESource", "CCoal", "CTraceEvidence",
                 "CPhantom"]

_CLASS_BY_ID = None


def class_table():
    """id -> class name, from the packaged API table (dante.symbols) + EXTRA_CLASSES."""
    global _CLASS_BY_ID
    if _CLASS_BY_ID is None:
        names = list(EXTRA_CLASSES)
        names += list(symbols.api_table().get("classes", ()))
        _CLASS_BY_ID = {}
        for n in names:
            _CLASS_BY_ID.setdefault(classid(n), n)
    return _CLASS_BY_ID


# ----------------------------------------------------------------------------
class Operand:
    """One decoded operand: its mode, its literal words, and the fixups on them."""
    __slots__ = ("mode", "words", "fix", "fix16", "label")

    def __init__(self, mode, words=(), fix=None, fix16=None, label=None):
        self.mode = mode
        self.words = tuple(words)
        self.fix = list(fix or [])
        self.fix16 = list(fix16 or [])
        self.label = label

    @property
    def nwords(self):
        return MODE_WORDS[self.mode]

    def value(self):
        if self.mode in (0,):
            return 0
        if self.mode == 1:
            return 1
        if self.mode in (2, 3):
            return s16(self.words[0])
        if self.mode in (4, 5, 6):
            return s32(*self.words)
        return s16(self.words[0])

    def copy(self):
        return Operand(self.mode, self.words, list(self.fix), list(self.fix16), self.label)


class Instr:
    __slots__ = ("off", "op", "ops", "comment")

    def __init__(self, op, ops=None, off=0, comment=""):
        self.op = op
        self.ops = list(ops) if ops else [Operand(0), Operand(0), Operand(0)]
        while len(self.ops) < 3:
            self.ops.append(Operand(0))
        self.off = off
        self.comment = comment

    @property
    def name(self):
        return OPNAME.get(self.op, "OP%d" % self.op)

    @property
    def size(self):
        return 1 + sum(o.nwords for o in self.ops)

    def words(self):
        w = [self.op | (self.ops[0].mode << 7) | (self.ops[1].mode << 10) | (self.ops[2].mode << 13)]
        for o in self.ops:
            w.extend(o.words)
        return w

    def word_index(self, idx):
        """word offset (relative to the instruction) of operand `idx`."""
        i = 1
        for k in range(idx):
            i += self.ops[k].nwords
        return i

    @staticmethod
    def decode(off, words):
        w = words[0]
        ins = Instr(w & 0x7F, off=off)
        modes = ((w >> 7) & 7, (w >> 10) & 7, (w >> 13) & 7)
        i = 1
        for k, m in enumerate(modes):
            n = MODE_WORDS[m]
            ins.ops[k] = Operand(m, words[i:i + n])
            i += n
        if i != len(words):
            raise ValueError("word count mismatch at %X" % off)
        return ins


# ----------------------------------------------------------------------------
def unescape(s):
    """The shipped writer escapes only backslash; quotes are stored raw."""
    out = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] == "\\":
            out.append("\\")
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def escape(s):
    return s.replace("\\", "\\\\")


def slot(v):
    return "L%X" % v if v >= 0 else "A%X" % (-v)


def parse_slot(t):
    if t[0] == "L":
        return int(t[1:], 16)
    if t[0] == "A":
        return -int(t[1:], 16)
    raise ValueError("bad slot %r" % t)


# ----------------------------------------------------------------------------
SEP = "//-----------------------------------------------------------"


class Dante:
    """A parsed .dante program.  `emit()` regenerates the file from the model."""

    def __init__(self, text=None, name=""):
        self.name = name
        self.version = 1
        self.data_size = 0
        self.code_size = 0
        self.assumptions = []   # (kind 'M'/'S', text, value)
        self.strings = []       # (data offset, text)
        self.code = []          # Instr, in order
        self.exports = []       # (kind 'C'/'D', offset, text)
        self.fixups = []        # (kind, text, [(codeoff, bits)])  -- parse order
        if text is not None:
            self.text = text
            self._parse(text)
        self._index()

    # -- parsing ------------------------------------------------------------
    def _parse(self, text):
        L = text.replace("\r\n", "\n").split("\n")
        i = 0
        hdr = []
        while i < len(L) and not L[i].startswith("BEGIN "):
            s = L[i].split("//")[0].strip()
            if s:
                hdr.append(int(s))
            i += 1
        self.version, self.data_size, self.code_size = hdr[:3]
        raw_fix = []
        while i < len(L):
            ln = L[i]
            if ln == "BEGIN ASSUMPTIONS":
                i += 1
                while L[i] != "END ASSUMPTIONS":
                    if L[i].strip():
                        m = re.match(r'^([MS]) "(.*)" (-?\d+)$', L[i])
                        self.assumptions.append((m.group(1), m.group(2), int(m.group(3))))
                    i += 1
            elif ln == "BEGIN STRINGS":
                i += 1
                while L[i] != "END STRINGS":
                    if L[i].strip():
                        m = re.match(r'^([0-9A-F]{8}) "(.*)"$', L[i])
                        self.strings.append((int(m.group(1), 16), unescape(m.group(2))))
                    i += 1
            elif ln == "BEGIN CODE":
                i += 1
                while L[i] != "END CODE":
                    if L[i].strip():
                        off, rest = L[i].split(":", 1)
                        words = [int(x, 16) for x in rest.split()]
                        self.code.append(Instr.decode(int(off, 16), words))
                    i += 1
            elif ln == "BEGIN EXPORTS":
                i += 1
                while L[i] != "END EXPORTS":
                    if L[i].strip():
                        m = re.match(r'^([CD]) ([0-9A-F]{8}) "(.*)"$', L[i])
                        self.exports.append((m.group(1), int(m.group(2), 16), m.group(3)))
                    i += 1
            elif ln == "BEGIN FIXUPS":
                i += 1
                while L[i] != "END FIXUPS":
                    if not L[i].strip():
                        i += 1
                        continue
                    m = re.match(r'^([CDIMNS]) "(.*)"$', L[i])
                    sites = []
                    i += 1
                    while i < len(L) and L[i].startswith("\t@"):
                        a = L[i][2:]
                        if "." in a:
                            a, bits = a.split(".")
                            sites.append((int(a, 16), int(bits)))
                        else:
                            sites.append((int(a, 16), 32))
                        i += 1
                    raw_fix.append((m.group(1), m.group(2), sites))
                    continue
            i += 1
        self.fixups = raw_fix
        self._attach_fixups()

    def _attach_fixups(self):
        """Move the FIXUPS table into the Operand objects it patches."""
        by_addr = collections.defaultdict(list)
        for k, sym, sites in self.fixups:
            for a, bits in sites:
                by_addr[a].append((k, sym, bits))
        for ins in self.code:
            for idx, o in enumerate(ins.ops):
                if not o.nwords:
                    continue
                wi = ins.word_index(idx)
                for k, sym, bits in by_addr.pop(ins.off + wi, []):
                    if bits != 32:
                        raise ValueError("%X: %d-bit fixup at operand start" % (ins.off + wi, bits))
                    o.fix.append((k, sym))
                if o.nwords == 2:
                    for k, sym, bits in by_addr.pop(ins.off + wi + 1, []):
                        if bits != 16:
                            raise ValueError("%X: %d-bit fixup on second word" % (ins.off + wi + 1, bits))
                        o.fix16.append((k, sym))
        if by_addr:
            raise ValueError("fixups outside operands: %s" % sorted(by_addr)[:5])

    def _index(self):
        self.by_off = {ins.off: ins for ins in self.code}
        self.func_at = {off: t for k, off, t in self.exports if k == "C"}
        self.global_at = {off: t for k, off, t in self.exports if k == "D"}
        self.string_at = dict(self.strings)
        self.stem_data = None
        for k, sym, sites in self.fixups:
            if k == "D" and sym.startswith("__") and sym.endswith("_data"):
                self.stem_data = sym
        if self.stem_data is None:
            for ins in self.code:
                for o in ins.ops:
                    for k, sym in o.fix:
                        if k == "D" and sym.startswith("__") and sym.endswith("_data"):
                            self.stem_data = sym
        self.fix_at = {}
        for k, sym, sites in self.fixups:
            for a, bits in sites:
                self.fix_at.setdefault(a, (k, sym, bits))

    # -- fixup regeneration --------------------------------------------------
    def collect_fixups(self):
        """Rebuild the FIXUPS table from the operands, in the shipped order:
        symbols sorted case-insensitively by their text, sites ascending."""
        table = collections.defaultdict(list)
        for ins in self.code:
            for idx, o in enumerate(ins.ops):
                if not o.nwords:
                    continue
                a = ins.off + ins.word_index(idx)
                for k, sym in o.fix:
                    table[(k, sym)].append((a, 32))
                for k, sym in o.fix16:
                    table[(k, sym)].append((a + 1, 16))
        out = []
        for (k, sym) in sorted(table, key=lambda ks: (ks[1].lower(), ks[0])):
            out.append((k, sym, sorted(table[(k, sym)])))
        return out

    def sort_sections(self):
        self.assumptions.sort(key=lambda a: a[1].lower())
        self.strings.sort(key=lambda s: s[0])
        self.exports.sort(key=lambda e: (e[0], e[1]))
        self.fixups = self.collect_fixups()

    # -- emission -----------------------------------------------------------
    def emit(self):
        out = []
        out.append("// DANTE compiled program")
        out.append("")
        out.append("%d  // File version" % self.version)
        out.append("%d  // VM data size" % self.data_size)
        out.append("%d  // VM code size" % self.code_size)
        out.append("")
        out.append(SEP)
        out.append("BEGIN ASSUMPTIONS")
        for k, t, v in self.assumptions:
            out.append('%s "%s" %d' % (k, t, v))
        out.append("")
        out.append("END ASSUMPTIONS")
        out.append("")
        out.append(SEP)
        out.append("BEGIN STRINGS")
        out.append("")
        for o, t in self.strings:
            out.append('%08X "%s"' % (o, escape(t)))
        out.append("")
        out.append("END STRINGS")
        out.append("")
        out.append(SEP)
        out.append("BEGIN CODE")
        for ins in self.code:
            out.append("%X: %s" % (ins.off, " ".join("%X" % (w & 0xFFFF) for w in ins.words())))
        out.append("")
        out.append("END CODE")
        out.append("")
        out.append(SEP)
        out.append("BEGIN EXPORTS")
        out.append("")
        for k, o, t in self.exports:
            out.append('%s %08X "%s"' % (k, o, t))
        out.append("")
        out.append("END EXPORTS")
        out.append("")
        out.append(SEP)
        out.append("BEGIN FIXUPS")
        out.append("")
        for k, sym, sites in self.fixups:
            out.append('%s "%s"' % (k, sym))
            for a, bits in sites:
                out.append("\t@%X" % a if bits == 32 else "\t@%X.%d" % (a, bits))
        out.append("")
        out.append("END FIXUPS")
        out.append("")
        out.append("END PROGRAM")
        out.append("")
        return "\r\n".join(out)

    # -- listing helpers ----------------------------------------------------
    def func_of(self, off):
        best = None
        for foff in self.func_at:
            if foff <= off and (best is None or foff > best):
                best = foff
        return best

    def data_ref(self, off):
        if off in self.string_at:
            return '"%s"' % escape(self.string_at[off])
        if off in self.global_at:
            return "$" + self.global_at[off].split(" ")[-1]
        for soff, s in self.strings:
            if soff < off < soff + len(s) + 1:
                return '"%s"+%d' % (escape(s), off - soff)
        return "data[%X]" % off

    def fmt_operand(self, ins, idx, role):
        o = ins.ops[idx]
        fx = (o.fix + o.fix16)
        if role == "-" and o.mode == 0:
            return None
        if o.mode == 0:
            return "#0"
        if o.mode == 1:
            return "#1"
        if o.mode == 2:
            v = s16(o.words[0])
            if role == "j":
                return "-> %X" % (ins.off + v)
            return "#%d" % v
        if o.mode == 3:
            return slot(s16(o.words[0]))
        if o.mode == 4:
            v = s32(*o.words)
            if o.fix:
                k, sym = o.fix[0]
                if k == "D" and sym == self.stem_data:
                    return self.data_ref(v)
                return "%s{%s}" % (("%+d" % v) if v else "", fxjoin(o.fix))
            if role == "f":
                return "fn@%X" % v
            if ins.op == 64 and idx == 2:
                cn = class_table().get(v & 0xFFFFFFFF)
                if cn:
                    return "#%d/%s" % (v, cn)
            f = bitsf(v)
            if v and abs(f) > 1e-6 and abs(f) < 1e7 and (v >> 23) & 0xFF not in (0, 0xFF):
                return "#%d/%gf" % (v, f)
            return "#%d" % v
        if o.mode == 5:
            v = s32(*o.words)
            if o.fix:
                k, sym = o.fix[0]
                if k == "D" and sym == self.stem_data and len(o.fix) == 1:
                    return "*" + self.data_ref(v)
                return "*%s{%s}" % (("%+d" % v) if v else "", fxjoin(o.fix))
            return "*handle(%08X)" % (v & 0xFFFFFFFF)
        if o.mode == 6:
            return slot(s32(*o.words))
        if o.mode == 7:
            base = s16(o.words[0])
            offv = s16(o.words[1])
            if o.fix16:
                return "[%s].%s" % (slot(base), o.fix16[0][1].split("::")[-1])
            return "[%s]+%d" % (slot(base), offv)
        return "?"

    def fmt_instr(self, ins):
        spec = OPS[ins.op] if ins.op < len(OPS) else ("OP%d" % ins.op, "?", "?", "?")
        name = spec[0]
        parts = []
        for idx in range(3):
            s = self.fmt_operand(ins, idx, spec[idx + 1])
            if s is not None:
                parts.append(s)
        if name == "CALL":
            tgt = parts[0] if parts else "?"
            fr = parts[1] if len(parts) > 1 else ""
            ar = parts[2] if len(parts) > 2 else ""
            fx = ins.ops[0].fix
            if fx and fx[0][0] == "N":
                return "CALL   %s args=%s" % (tgt, ar)
            if fx and fx[0][0] == "C":
                return "CALL   %s frame=%s" % (tgt, fr)
            return "CALL   %s frame=%s args=%s" % (tgt, fr, ar)
        return "%-7s %s" % (name, ", ".join(parts))


def fxjoin(fixes):
    return " ".join("%s:%s" % (k, sym) for k, sym in fixes)


# ----------------------------------------------------------------------------
# Assembly source (.s): a complete, re-assemblable rendering of a module.
ASM_HEADER = "; Dante assembly -- dante disasm --asm / dante asm"


def asm_operand_text(d, ins, idx, role):
    o = ins.ops[idx]
    if o.mode == 0:
        base = "#0"
    elif o.mode == 1:
        base = "#1"
    elif o.mode == 2:
        if role == "j":
            base = "-> L_%X" % (ins.off + s16(o.words[0]))
        else:
            base = "#%d" % s16(o.words[0])
    elif o.mode == 3:
        base = slot(s16(o.words[0]))
    elif o.mode in (4, 5):
        v = s32(*o.words)
        if o.mode == 4 and ins.op == 64 and idx == 2 and not o.fix:
            cn = class_table().get(v & 0xFFFFFFFF)
            base = "##class:%s" % cn if cn else "##%d" % v
        elif o.mode == 4 and not o.fix and role != "f":
            f = bitsf(v)
            if v and abs(f) > 1e-6 and abs(f) < 1e7 and (v >> 23) & 0xFF not in (0, 0xFF):
                base = "##f:%r" % f
            else:
                base = "##%d" % v
        else:
            base = ("##%d" if o.mode == 4 else "*%d") % v
    elif o.mode == 6:
        base = "%s:32" % slot(s32(*o.words))
    elif o.mode == 7:
        base = "[%s]+%d" % (slot(s16(o.words[0])), s16(o.words[1]))
    else:
        raise ValueError("mode %d" % o.mode)
    for k, sym in o.fix:
        base += '{%s:"%s"}' % (k, escape(sym))
    for k, sym in o.fix16:
        base += '{%s16:"%s"}' % (k, escape(sym))
    return base


def to_asm(d):
    """Render a parsed Dante module as assembly source."""
    labels = {}
    for ins in d.code:
        spec = OPS[ins.op]
        if spec[1] == "j" and ins.ops[0].mode == 2:
            labels[ins.off + s16(ins.ops[0].words[0])] = True
    out = [ASM_HEADER, ""]
    out.append(".version %d" % d.version)
    out.append(".datasize %d" % d.data_size)
    out.append(".codesize %d" % d.code_size)
    if d.name:
        out.append(".module %s" % d.name)
    out.append("")
    for k, t, v in d.assumptions:
        out.append('.assume %s "%s" %d' % (k, escape(t), v))
    out.append("")
    for o, t in d.strings:
        out.append('.string %08X "%s"' % (o, escape(t)))
    out.append("")
    for k, o, t in d.exports:
        if k == "D":
            out.append('.global %08X "%s"' % (o, escape(t)))
    out.append("")
    out.append(".code")
    for ins in d.code:
        if ins.off in d.func_at:
            out.append("")
            out.append('.func "%s"' % escape(d.func_at[ins.off]))
        if ins.off in labels:
            out.append("L_%X:" % ins.off)
        spec = OPS[ins.op]
        parts = [asm_operand_text(d, ins, i, spec[i + 1]) for i in range(3)]
        while len(parts) > 1 and parts[-1] == "#0":
            parts.pop()
        if parts == ["#0"]:
            parts = []
        out.append("\t%-7s %s" % (spec[0], ", ".join(parts)))
    out.append("")
    return "\n".join(out)


# ----------------------------------------------------------------------------
_RE_FIX = re.compile(r'\{([A-Z]+)(16)?:"((?:[^"\\]|\\.)*)"\}')


def parse_asm_operand(tok, role, ctx):
    """tok -> Operand.  ctx is used only for error messages."""
    fixes = []
    fixes16 = []
    def eat(m):
        k, w16, sym = m.group(1), m.group(2), unescape(m.group(3))
        (fixes16 if w16 else fixes).append((k, sym))
        return ""
    core = _RE_FIX.sub(eat, tok).strip()
    if core.startswith("->"):
        o = Operand(2, (0,), fixes, fixes16, label=core[2:].strip())
        return o
    if core == "#0":
        o = Operand(0, ())
    elif core == "#1":
        o = Operand(1, ())
    elif core.startswith("##"):
        body = core[2:]
        if body.startswith("f:"):
            v = fbits(float(body[2:]))
        elif body.startswith("class:"):
            v = classid(body[6:])
        elif body.startswith("0x") or body.startswith("-0x"):
            v = int(body, 16)
        else:
            v = int(body)
        o = Operand(4, w32(v))
    elif core.startswith("#"):
        o = Operand(2, (int(core[1:]) & 0xFFFF,))
    elif core.startswith("*"):
        body = core[1:]
        v = int(body, 16) if body.startswith(("0x", "-0x")) else int(body)
        o = Operand(5, w32(v))
    elif core.startswith("["):
        m = re.match(r'^\[([LA][0-9A-F]+)\]\+(-?\d+)$', core)
        if not m:
            raise ValueError("%s: bad indirect operand %r" % (ctx, core))
        o = Operand(7, (parse_slot(m.group(1)) & 0xFFFF, int(m.group(2)) & 0xFFFF))
    elif re.match(r'^[LA][0-9A-F]+:32$', core):
        o = Operand(6, w32(parse_slot(core[:-3])))
    elif re.match(r'^[LA][0-9A-F]+$', core):
        o = Operand(3, (parse_slot(core) & 0xFFFF,))
    else:
        raise ValueError("%s: bad operand %r" % (ctx, core))
    o.fix = fixes
    o.fix16 = fixes16
    return o


def split_operands(s):
    """Split on commas that are not inside quotes or brackets."""
    out, depth, q, cur = [], 0, False, ""
    i = 0
    while i < len(s):
        c = s[i]
        if q:
            cur += c
            if c == "\\" and i + 1 < len(s):
                cur += s[i + 1]
                i += 2
                continue
            if c == '"':
                q = False
        elif c == '"':
            q = True
            cur += c
        elif c in "[{":
            depth += 1
            cur += c
        elif c in "]}":
            depth -= 1
            cur += c
        elif c == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += c
        i += 1
    if cur.strip():
        out.append(cur.strip())
    return out


def parse_asm(text, name=""):
    d = Dante(None, name)
    labels = {}
    pending_export = []
    in_code = False
    for lineno, raw in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        ctx = "%s:%d" % (name or "<asm>", lineno)
        st = line.strip()
        if st.startswith("."):
            kw, _, rest = st.partition(" ")
            rest = rest.strip()
            if kw == ".version":
                d.version = int(rest)
            elif kw == ".datasize":
                d.data_size = int(rest)
            elif kw == ".codesize":
                pass  # recomputed by layout
            elif kw == ".module":
                d.name = rest
            elif kw == ".assume":
                m = re.match(r'^([MS]) "((?:[^"\\]|\\.)*)" (-?\d+)$', rest)
                if not m:
                    raise ValueError("%s: bad .assume" % ctx)
                d.assumptions.append((m.group(1), unescape(m.group(2)), int(m.group(3))))
            elif kw == ".string":
                m = re.match(r'^([0-9A-Fa-f]+) "(.*)"$', rest)
                d.strings.append((int(m.group(1), 16), unescape(m.group(2))))
            elif kw == ".global":
                m = re.match(r'^([0-9A-Fa-f]+) "(.*)"$', rest)
                d.exports.append(("D", int(m.group(1), 16), unescape(m.group(2))))
            elif kw == ".func":
                m = re.match(r'^"(.*)"$', rest)
                pending_export.append(unescape(m.group(1)))
            elif kw == ".code":
                in_code = True
            else:
                raise ValueError("%s: unknown directive %r" % (ctx, kw))
            continue
        if st.endswith(":") and " " not in st[:-1]:
            labels[st[:-1]] = len(d.code)
            continue
        # instruction
        mn, _, argtxt = st.partition(" ")
        if mn not in NAMEOP:
            raise ValueError("%s: unknown mnemonic %r" % (ctx, mn))
        op = NAMEOP[mn]
        spec = OPS[op]
        toks = split_operands(argtxt)
        ops = []
        for i in range(3):
            if i < len(toks):
                ops.append(parse_asm_operand(toks[i], spec[i + 1], ctx))
            else:
                ops.append(Operand(0))
        ins = Instr(op, ops)
        if pending_export:
            for proto in pending_export:
                d.exports.append(("C", len(d.code), proto))   # index for now
            pending_export = []
        d.code.append(ins)
    if pending_export:
        raise ValueError("%s: .func with no instruction after it" % (name or "<asm>"))
    # layout
    off = 0
    for ins in d.code:
        ins.off = off
        off += ins.size
    d.code_size = off
    idx_off = [ins.off for ins in d.code] + [off]
    d.exports = [(k, (idx_off[o] if k == "C" else o), t) for k, o, t in d.exports]
    # resolve labels
    for ins in d.code:
        for o in ins.ops:
            if o.label is not None:
                if o.label not in labels:
                    raise ValueError("undefined label %r" % o.label)
                tgt = idx_off[labels[o.label]]
                o.words = ((tgt - ins.off) & 0xFFFF,)
    d.sort_sections()
    d._index()
    return d


def _strip_comment(raw):
    out, q, i = "", False, 0
    while i < len(raw):
        c = raw[i]
        if q:
            out += c
            if c == "\\" and i + 1 < len(raw):
                out += raw[i + 1]
                i += 2
                continue
            if c == '"':
                q = False
        elif c == '"':
            q = True
            out += c
        elif c == ";":
            break
        else:
            out += c
        i += 1
    return out


# ----------------------------------------------------------------------------
def load(path):
    with open(path, "r", encoding="latin-1", newline="") as f:
        text = f.read()
    return Dante(text, os.path.splitext(os.path.basename(path))[0])


def iter_files(target):
    if os.path.isdir(target):
        return sorted(glob.glob(os.path.join(target, "**", "*.dante"), recursive=True))
    return [target]


def cmd_disasm(args):
    d = load(args.file)
    if args.asm:
        text = to_asm(d)
        if args.out:
            with open(args.out, "w", newline="\n") as f:
                f.write(text)
        else:
            sys.stdout.write(text)
        return
    only = args.func
    labels = set()
    for ins in d.code:
        spec = OPS[ins.op] if ins.op < len(OPS) else None
        if spec and spec[1] == "j" and ins.ops[0].mode == 2:
            labels.add(ins.off + s16(ins.ops[0].words[0]))
    cur = None
    out = []
    for ins in d.code:
        f = d.func_of(ins.off)
        if f != cur:
            cur = f
            if only is None or only in (d.func_at.get(f) or ""):
                out.append("")
                out.append("; ---- %s  @%X" % (d.func_at.get(f, "?"), f if f is not None else 0))
        if only is not None and only not in (d.func_at.get(f) or ""):
            continue
        lab = "L_%X:" % ins.off if ins.off in labels else ""
        raw = " ".join("%X" % w for w in ins.words()) if args.raw else ""
        out.append("%-10s %5X:  %-40s %s" % (lab, ins.off, d.fmt_instr(ins), ("; " + raw) if raw else ""))
    print("\n".join(out))


def verify_one(d, quiet=False):
    errs = []
    offs = set(d.by_off)
    expect = 0
    for ins in d.code:
        if ins.off != expect:
            errs.append("gap/overlap at %X (expected %X)" % (ins.off, expect))
        if ins.op >= len(OPS):
            errs.append("%X: bad opcode %d" % (ins.off, ins.op))
        expect = ins.off + ins.size
    if expect != d.code_size:
        errs.append("code size %d != header %d" % (expect, d.code_size))
    for ins in d.code:
        spec = OPS[ins.op] if ins.op < len(OPS) else None
        if spec and spec[1] == "j":
            o = ins.ops[0]
            if o.mode != 2:
                errs.append("%X: jump with dest mode %d" % (ins.off, o.mode))
            elif ins.off + s16(o.words[0]) not in offs and ins.op != 5:
                errs.append("%X: jump to non-boundary %X" % (ins.off, ins.off + s16(o.words[0])))
    for ins in d.code:
        for idx, o in enumerate(ins.ops):
            if o.fix and o.mode not in (4, 5):
                errs.append("%X: 32-bit fixup on mode %d (%s)" % (ins.off, o.mode, ins.name))
            if o.fix16 and o.mode != 7:
                errs.append("%X: 16-bit fixup on mode %d (%s)" % (ins.off, o.mode, ins.name))
    for k, off, txt in d.exports:
        if k == "C":
            ins = d.by_off.get(off)
            if ins is None:
                errs.append("export %s at non-boundary %X" % (txt, off))
            elif ins.op != 1:
                errs.append("export %s at %X starts with %s" % (txt, off, ins.name))
    # every D global must fit inside the declared data segment
    for k, off, txt in d.exports:
        if k == "D" and off >= d.data_size:
            errs.append("global %s at %X beyond data size %d" % (txt, off, d.data_size))
    # STRINGS must be one contiguous run of 4-byte slots, with the D globals after it.
    soffs = [off for off, _ in d.strings]
    for a, b in zip(soffs, soffs[1:]):
        if b != a + 4:
            errs.append("STRINGS not contiguous: %X follows %X (expected %X)" % (b, a, a + 4))
            break
    if soffs:
        send = soffs[-1] + 4
        for k, off, txt in d.exports:
            if k == "D" and off < send:
                errs.append("global %s at %X overlaps the STRINGS table (ends %X)" % (txt, off, send))
                break
    if not quiet:
        for e in errs[:40]:
            print("  " + e)
    return errs


def cmd_verify(args):
    bad = 0
    for p in iter_files(args.target):
        d = load(p)
        errs = verify_one(d, quiet=True)
        print("%-24s %6d instrs  %5d fixup sites  %s" % (
            os.path.basename(p), len(d.code), sum(len(s) for _, _, s in d.fixups),
            "OK" if not errs else "%d ERRORS" % len(errs)))
        for e in errs[:10]:
            print("    " + e)
        bad += bool(errs)
    return 1 if bad else 0


def cmd_stats(args):
    ops = collections.Counter()
    for p in iter_files(args.target):
        d = load(p)
        for ins in d.code:
            ops[ins.name] += 1
    for k, v in ops.most_common():
        print("%-8s %7d" % (k, v))


def cmd_roundtrip(args):
    bad = 0
    n = 0
    for p in iter_files(args.target):
        d = load(p)
        d.sort_sections()
        with open(p, "rb") as f:
            orig = f.read()
        out = d.emit().encode("latin-1")
        ok = out == orig
        bad += not ok
        n += 1
        print("%-24s %s" % (os.path.basename(p), "identical" if ok else "DIFFERENT"))
    print("%d/%d identical" % (n - bad, n))
    return 1 if bad else 0


def cmd_roundtrip_asm(args):
    bad = 0
    n = 0
    for p in iter_files(args.target):
        d = load(p)
        s = to_asm(d)
        d2 = parse_asm(s, d.name)
        with open(p, "rb") as f:
            orig = f.read()
        out = d2.emit().encode("latin-1")
        ok = out == orig
        bad += not ok
        n += 1
        msg = "identical"
        if not ok:
            msg = "DIFFERENT"
            if args.diff:
                a = orig.decode("latin-1").split("\r\n")
                b = out.decode("latin-1").split("\r\n")
                import difflib
                for i, ln in enumerate(difflib.unified_diff(a, b, lineterm="", n=1)):
                    if i > 30:
                        break
                    print("   " + ln)
        print("%-24s %s" % (os.path.basename(p), msg))
    print("%d/%d identical" % (n - bad, n))
    return 1 if bad else 0


def cmd_asm(args):
    with open(args.file, "r", encoding="latin-1") as f:
        text = f.read()
    d = parse_asm(text, os.path.splitext(os.path.basename(args.file))[0])
    with open(args.out, "wb") as f:
        f.write(d.emit().encode("latin-1"))
    errs = verify_one(d, quiet=True)
    print("%s: %d instrs, %d words, %d fixup sites%s" % (
        args.out, len(d.code), d.code_size, sum(len(s) for _, _, s in d.fixups),
        "" if not errs else "  (%d VERIFY ERRORS)" % len(errs)))
    for e in errs[:10]:
        print("   " + e)
    return 1 if errs else 0


def cmd_classid(args):
    for n in args.names:
        v = classid(n)
        print("%-28s 0x%08X  %d" % (n, v, classid_signed(n)))


# ----------------------------------------------------------------------------
# link: splice separately assembled code into an existing module
def link(base, patch, hooks=(), verbose=True):
    """Append `patch`'s code/data/exports to `base` and install `hooks`."""
    stem = base.stem_data
    if stem is None:
        raise SystemExit("link: %s has no __<stem>_data symbol" % base.name)
    for k, off, txt in patch.exports:
        if k == "C" and any(e[0] == "C" and e[2] == txt for e in base.exports):
            raise SystemExit("link: %s already exports %r" % (base.name, txt))
    # 1. Relocate the patch's data.  STRINGS has to stay contiguous, so new
    #    literals go after the base's last one and everything past there shifts up.
    smap, gmap = {}, {}
    sbase = max([off for off, _ in base.strings], default=0) + (4 if base.strings else 0)
    shift = 4 * len(patch.strings)
    if shift:
        base.exports = [("D", off + shift, txt) if (k == "D" and off >= sbase) else (k, off, txt)
                        for k, off, txt in base.exports]
        for ins in base.code:
            for o in ins.ops:
                if any(k == "D" and sym == stem for k, sym in o.fix) and len(o.words) == 2:
                    v = s32(*o.words)
                    if v >= sbase:
                        o.words = w32(v + shift)
    for off, txt in patch.strings:
        smap[off] = sbase
        base.strings.append((sbase, txt))
        sbase += 4
    dbase = base.data_size + shift
    for k, off, txt in patch.exports:
        if k == "D":
            gmap[off] = dbase
            base.exports.append(("D", dbase, txt))
            dbase += 12 if txt.split(" ")[0] == "Vector" else 4
    base.data_size = dbase
    for ins in patch.code:
        for o in ins.ops:
            for i, (k, sym) in enumerate(o.fix):
                if k == "D" and sym.startswith("__") and sym.endswith("_data"):
                    o.fix[i] = (k, stem)
                    v = s32(*o.words)
                    if v in smap:
                        o.words = w32(smap[v])
                    elif v in gmap:
                        o.words = w32(gmap[v])
    # 2. make every address reference symbolic, then splice
    _labelize(base)
    _labelize(patch)
    base.code.extend(patch.code)
    base._export_refs.extend([r for r in patch._export_refs if r[0] == "C"])
    # 3. hooks: `CALL <callee>` immediately after the target function's ENTER
    for target_proto, call_proto, frame in hooks:
        idx = None
        for i, ins in enumerate(base.code):
            for k, ref, txt in base._export_refs:
                if k == "C" and ref is ins and txt == target_proto:
                    idx = i
        if idx is None:
            raise SystemExit("link: no export %r in %s" % (target_proto, base.name))
        enter = base.code[idx]
        if enter.op != NAMEOP["ENTER"]:
            raise SystemExit("link: export %r does not start with ENTER" % target_proto)
        need = max(s16(enter.ops[0].words[0]), frame + 8)
        enter.ops[0] = Operand(2, (need & 0xFFFF,))
        call = Instr(NAMEOP["CALL"], [
            Operand(4, (0, 0), fix=[("C", call_proto)]),
            Operand(2, (frame & 0xFFFF,)),
            Operand(2, (0xFFFF,)),
        ])
        base.code.insert(idx + 1, call)
        if verbose:
            print("   hook: %s -> CALL %s (frame #%d, ENTER #%d)" % (
                target_proto, call_proto, frame, need))
    _relayout(base)
    return base


def _labelize(d):
    """Convert every jump displacement into a symbolic label bound to an Instr."""
    tgt = {}
    for ins in d.code:
        spec = OPS[ins.op]
        if spec[1] == "j" and ins.ops[0].mode == 2:
            tgt[ins.off + s16(ins.ops[0].words[0])] = True
    byoff = {ins.off: ins for ins in d.code}
    for ins in d.code:
        spec = OPS[ins.op]
        if spec[1] == "j" and ins.ops[0].mode == 2:
            a = ins.off + s16(ins.ops[0].words[0])
            ins.ops[0].label = byoff.get(a) or a
    # exports become instruction references too
    d._export_refs = [(k, (byoff.get(off) if k == "C" else off), txt)
                      for k, off, txt in d.exports]


def _relayout(d):
    off = 0
    for ins in d.code:
        ins.off = off
        off += ins.size
    d.code_size = off
    for ins in d.code:
        o = ins.ops[0]
        if o.label is not None:
            tgt = o.label.off if isinstance(o.label, Instr) else o.label
            o.words = ((tgt - ins.off) & 0xFFFF,)
    if hasattr(d, "_export_refs"):
        d.exports = [(k, (r.off if isinstance(r, Instr) else r), t) for k, r, t in d._export_refs]
    d.sort_sections()
    d._index()


def func_body(d, proto):
    """The instruction list of one `C` export, up to the next one."""
    offs = sorted(d.func_at)
    start = None
    for o in offs:
        if d.func_at[o] == proto:
            start = o
    if start is None:
        return None
    nxt = min([o for o in offs if o > start], default=d.code_size)
    return [ins for ins in d.code if start <= ins.off < nxt]


def normalize(d, ins):
    """Instruction words + fixups, with this module's own string-pool offsets blanked
    (they are the only thing allowed to differ between two builds of one module)."""
    words = list(ins.words())
    tags = []
    for idx, o in enumerate(ins.ops):
        if not o.nwords:
            continue
        wi = ins.word_index(idx)
        for k, sym in o.fix:
            tags.append((idx, k, sym))
            if k == "D" and sym == d.stem_data:
                words[wi] = words[wi + 1] = "<str>"
        for k, sym in o.fix16:
            tags.append((idx, k + "16", sym))
    return (ins.name, tuple(words), tuple(sorted(tags)))


def call_targets(d, proto):
    """The ordered call graph of one export: every `C`/`N` fixup symbol in its
    body (CALL/THREAD targets, function values, CTOR destructors)."""
    body = func_body(d, proto)
    if body is None:
        return None
    out = []
    for ins in body:
        for o in ins.ops:
            for k, sym in o.fix:
                if k in ("C", "N"):
                    out.append((ins.name, k, sym))
    return out


def compare_calls(a, b):
    """Exports whose call graph changed between two builds of one module."""
    changed = []
    for off in sorted(a.func_at):
        proto = a.func_at[off]
        x = call_targets(a, proto)
        y = call_targets(b, proto)
        if y is None or x != y:
            changed.append(proto)
    return changed


def compare_modules(a, b, verbose=False):
    """Compare every C export of `a` against `b` word for word.  Returns
    (identical, differing, missing) lists of prototypes."""
    same, diff, missing = [], [], []
    for off in sorted(a.func_at):
        proto = a.func_at[off]
        x = func_body(a, proto)
        y = func_body(b, proto)
        if y is None:
            missing.append(proto)
            continue
        nx = [normalize(a, i) for i in x]
        ny = [normalize(b, i) for i in y]
        if nx == ny:
            same.append(proto)
            continue
        diff.append(proto)
        if verbose:
            print("DIFFERS %s   (%d vs %d instr)" % (proto, len(x), len(y)))
            shown = 0
            for i in range(max(len(nx), len(ny))):
                p = nx[i] if i < len(nx) else None
                q = ny[i] if i < len(ny) else None
                if p != q:
                    print("    a #%d: %s" % (i, a.fmt_instr(x[i]) if i < len(x) else "-"))
                    print("    b #%d: %s" % (i, b.fmt_instr(y[i]) if i < len(y) else "-"))
                    shown += 1
                    if shown >= 3:
                        break
    return same, diff, missing


def cmd_compare(args):
    a = load(args.a)
    b = load(args.b)
    same, diff, missing = compare_modules(a, b, verbose=args.verbose)
    extra = [p for p in b.func_at.values() if p not in a.func_at.values()]
    calls = compare_calls(a, b)
    print("%s vs %s: %d exports identical, %d differ, %d missing in b, %d only in b" % (
        os.path.basename(args.a), os.path.basename(args.b), len(same), len(diff), len(missing), len(extra)))
    print("  call targets: %d export(s) changed" % len(calls))
    for p in diff:
        print("  differs: %s" % p)
    for p in missing:
        print("  missing: %s" % p)
    for p in calls:
        print("  call-targets-changed: %s" % p)
    return 0 if not diff and not missing else 1


def cmd_link(args):
    base = load(args.base)
    hooks = []
    for h in args.hook or []:
        tgt, _, callee = h.partition("=")
        hooks.append((tgt, callee, args.frame))
    for i, pth in enumerate(args.add or []):
        if pth.endswith(".dante"):
            patch = load(pth)
        else:
            with open(pth, "r", encoding="latin-1") as f:
                patch = parse_asm(f.read(), os.path.splitext(os.path.basename(pth))[0])
        link(base, patch, hooks if i == 0 else [])
    with open(args.out, "wb") as f:
        f.write(base.emit().encode("latin-1"))
    errs = verify_one(base, quiet=True)
    print("%s: %d instrs, %d words, data %d, %d fixup sites%s" % (
        args.out, len(base.code), base.code_size, base.data_size,
        sum(len(s) for _, _, s in base.fixups),
        "" if not errs else "  (%d VERIFY ERRORS)" % len(errs)))
    for e in errs[:10]:
        print("   " + e)
    return 1 if errs else 0


def register(sub):
    """Mount the container subcommands on the `dante` CLI."""
    a = sub.add_parser("disasm", help="annotated listing, or re-assemblable source with --asm")
    a.add_argument("file"); a.add_argument("--func")
    a.add_argument("--raw", action="store_true"); a.add_argument("--asm", action="store_true")
    a.add_argument("-o", "--out"); a.set_defaults(fn=cmd_disasm)
    a = sub.add_parser("asm", help="assemble a .s back into a .dante")
    a.add_argument("file"); a.add_argument("-o", "--out", required=True)
    a.set_defaults(fn=cmd_asm)
    a = sub.add_parser("verify", help="structural verification")
    a.add_argument("target"); a.set_defaults(fn=cmd_verify)
    a = sub.add_parser("stats", help="opcode / operand-mode histogram")
    a.add_argument("target"); a.set_defaults(fn=cmd_stats)
    a = sub.add_parser("roundtrip", help="parse -> re-emit -> byte compare")
    a.add_argument("target"); a.set_defaults(fn=cmd_roundtrip)
    a = sub.add_parser("roundtrip-asm", help="parse -> .s -> assemble -> byte compare")
    a.add_argument("target")
    a.add_argument("--diff", action="store_true"); a.set_defaults(fn=cmd_roundtrip_asm)
    a = sub.add_parser("link", help="append code/data to a module and hook functions")
    a.add_argument("base"); a.add_argument("--add", action="append")
    a.add_argument("--hook", action="append", help='"void setupLevel()=void myFunc()"')
    a.add_argument("--frame", type=int, default=8,
                   help="callee frame offset for an injected CALL (default 8)")
    a.add_argument("-o", "--out", required=True); a.set_defaults(fn=cmd_link)
    a = sub.add_parser("classid", help="the DYNCAST class id of a name")
    a.add_argument("names", nargs="+"); a.set_defaults(fn=cmd_classid)
    a = sub.add_parser("compare", help="compare the exports of two builds of one module word for word")
    a.add_argument("a"); a.add_argument("b"); a.add_argument("--verbose", action="store_true")
    a.set_defaults(fn=cmd_compare)
