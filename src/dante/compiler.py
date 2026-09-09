"""A C-like compiler for the Dante VM: `.dn` source to a module or to assembly.

It reproduces the shipped compiler's frame layout, so output compares word for word."""
import os
import re
import sys
import json
import glob

from . import symbols
from . import module as DV
from .module import Operand, Instr, Dante, NAMEOP, w32, fbits, classid


# ============================================================ types
VEC = "Vector"


class Type:
    """A Dante type, identified by its prototype spelling."""
    __slots__ = ("txt", "kind", "size", "inner", "ret", "params")

    def __init__(self, txt, kind, size, inner=None, ret=None, params=None):
        self.txt = txt
        self.kind = kind
        self.size = size
        self.inner = inner
        self.ret = ret
        self.params = params

    def __repr__(self):
        return "Type(%s)" % self.txt

    def __eq__(self, o):
        return isinstance(o, Type) and o.txt == self.txt

    def __hash__(self):
        return hash(self.txt)

    @property
    def is_num(self):
        return self.kind in ("int", "float")

    @property
    def slots(self):
        return self.size // 4


T_VOID = Type("void", "void", 0)
T_INT = Type("int", "int", 4)
T_FLOAT = Type("float", "float", 4)
T_BOOL = Type("bool", "bool", 4)
T_STRING = Type("String", "String", 4)
T_VEC = Type("Vector", "Vector", 12)
PRIMS = {t.txt: t for t in (T_VOID, T_INT, T_FLOAT, T_BOOL, T_STRING, T_VEC)}
# primitive keyword spellings are case-insensitive 
PRIMS_LOWER = {k.lower(): k for k in PRIMS}


def split_top(s, sep=","):
    out, depth, cur = [], 0, ""
    for c in s:
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == sep and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += c
    if cur.strip():
        out.append(cur.strip())
    return out


class CompileError(Exception):
    pass


def err(msg, tok=None):
    raise CompileError("%s%s" % (("line %d: " % tok.line) if tok else "", msg))


# ============================================================ symbol database
def _split_default(part):
    """`"bool=false"` -> `("bool", "false")`; `"bool"` -> `("bool", None)`.
    Depth-aware so a function-type param `(void())` is untouched."""
    depth = 0
    for i, c in enumerate(part):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "=" and depth == 0:
            return part[:i].strip(), part[i + 1:].strip()
    return part, None


def _parse_default_literal(text, line=0):
    """The trailing `=<literal>` of a prototype parameter, as an AST node."""
    t = text.strip()
    if t == "true":
        return Node("bool", v=True, line=line)
    if t == "false":
        return Node("bool", v=False, line=line)
    if t == "null":
        return Node("null", line=line)
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return Node("str", v=t[1:-1], line=line)
    try:
        if re.match(r"^-?\d+$", t):
            return Node("int", v=int(t), line=line)
        return Node("float", v=float(t[:-1] if t.lower().endswith("f") else t), line=line)
    except ValueError:
        raise CompileError("bad default value %r in prototype" % text)


class Proto:
    """A parsed prototype string: `<ret> [Class::]name[{@Class}](<params>)`."""
    __slots__ = ("text", "ret", "cls", "name", "params", "this", "defaults")

    def __init__(self, text, types):
        self.text = text
        s = text.strip()
        if not s.endswith(")"):
            raise CompileError("bad prototype %r" % text)
        depth = 0
        for i in range(len(s) - 1, -1, -1):
            if s[i] == ")":
                depth += 1
            elif s[i] == "(":
                depth -= 1
                if depth == 0:
                    break
        head, plist = s[:i], s[i + 1:-1]
        # split head at last top-level space -> ret, name
        depth = 0
        cut = -1
        for j, c in enumerate(head):
            if c in "([":
                depth += 1
            elif c in ")]":
                depth -= 1
            elif c == " " and depth == 0:
                cut = j
        if cut < 0:
            raise CompileError("bad prototype %r" % text)
        self.ret = types.parse(head[:cut].strip())
        nm = head[cut + 1:].strip()
        self.this = None
        m = re.match(r"^(.*)\{(@[A-Za-z0-9_]+)\}$", nm)
        if m:
            nm, self.this = m.group(1), types.parse(m.group(2))
        if "::" in nm:
            self.cls, self.name = nm.split("::", 1)
        else:
            self.cls, self.name = None, nm
        self.params = []
        self.defaults = {}
        if plist.strip():
            for idx, part in enumerate(split_top(plist)):
                type_txt, def_txt = _split_default(part)
                self.params.append(types.parse(type_txt))
                if def_txt is not None:
                    self.defaults[idx] = _parse_default_literal(def_txt)
        if self.defaults:
            # self.text ends up as a fixup target, so it has to stay the plain
            # prototype the runtime hashes -- never carrying our `=default` notes.
            namepart = ("%s::%s" % (self.cls, self.name)) if self.cls else self.name
            if self.this is not None:
                namepart += "{%s}" % self.this.txt
            self.text = "%s %s(%s)" % (self.ret.txt, namepart,
                                        ",".join(t.txt for t in self.params))

    def __repr__(self):
        return "Proto(%s)" % self.text


def attach_defaults(proto, params):
    """Copy parsed parameter defaults onto a Proto so emit_call can pad short calls."""
    for i, (_, _, default) in enumerate(params):
        if default is not None:
            proto.defaults[i] = default


def params_match(p, n):
    """True if a call with `n` arguments can resolve to prototype `p`: either
    an exact match, or `n` short and every parameter beyond it has a default."""
    if len(p.params) == n:
        return True
    return n < len(p.params) and all(i in p.defaults for i in range(n, len(p.params)))


class Types:
    """Type interner; knows struct sizes and member offsets."""

    def __init__(self):
        self.cache = dict(PRIMS)
        self.lower = {k.lower(): k for k in self.cache}   # lower text -> canonical text
        self.sizeof = {}          # struct name -> size (script units)
        self.members = {}         # (struct, member) -> (Type, offset)

    def parse(self, txt):
        txt = txt.strip()
        if txt in self.cache:
            return self.cache[txt]
        canon = self.lower.get(txt.lower())
        if canon is not None:
            # Authors spell type names freely, so alias a miscased one to the DB's
            # spelling; otherwise it gets 4 bytes and no ctor, wrecking the frame.
            print("dante compile: warning: type %r taken as %r (case-insensitive match)"
                  % (txt, canon), file=sys.stderr)
            t = self.cache[canon]
            self.cache[txt] = t
            return t
        if txt.startswith("("):
            inner = txt[1:-1].strip()
            # function type: `<ret>(<params>)`
            depth = 0
            cut = None
            for i in range(len(inner) - 1, -1, -1):
                if inner[i] == ")":
                    depth += 1
                elif inner[i] == "(":
                    depth -= 1
                    if depth == 0:
                        cut = i
                        break
            ret = self.parse(inner[:cut])
            ps = [self.parse(x) for x in split_top(inner[cut + 1:-1])]
            t = Type(txt, "func", 4, ret=ret, params=ps)
        elif txt.startswith("@"):
            inner = txt[1:]
            kind = "obj" if inner[:1] == "C" and inner[1:2].isupper() else "ptr"
            t = Type(txt, kind, 4, inner=inner)
        elif re.match(r"^E[A-Z]", txt):
            t = Type(txt, "enum", 4)
        elif re.match(r"^S[A-Z]", txt):
            t = Type(txt, "struct", self.sizeof.get(txt, 4))
        elif re.match(r"^C[A-Z]", txt):
            # a value object (no `@`); as a local it occupies sizeof(CFoo) if known
            t = Type(txt, "cls", self.sizeof.get(txt, 4), inner=txt)
        else:
            t = Type(txt, "opaque", 4)
        self.cache[txt] = t
        self.lower.setdefault(txt.lower(), txt)
        return t

    def set_sizeof(self, name, size):
        self.sizeof[name] = size
        if name in self.cache:
            self.cache[name].size = size
        else:
            self.parse(name).size = size

    def member(self, stype, name):
        return self.members.get((stype, name))


class SymbolDB:
    """The native, enum and script-function tables a module compiles against.

    by_name and scriptfns are case-sensitive; folding them changes which overloads tie."""

    def __init__(self, types):
        self.types = types
        self.natives = []          # Proto
        self.declared = set()      # prototypes named by the module's own `native` lines
        self.by_name = {}          # (cls or None, name) -> [Proto]
        self.enums = {}            # constant.lower() -> "EType constant" (canonical case)
        self.scriptfns = {}        # name -> [Proto]

    def add_native(self, text, declared=False):
        try:
            p = Proto(text, self.types)
        except CompileError:
            return
        self.natives.append(p)
        self.by_name.setdefault((p.cls, p.name), []).append(p)
        if declared:
            self.declared.add(p.text)

    def add_script(self, text):
        p = Proto(text, self.types)
        self.scriptfns.setdefault(p.name, []).append(p)
        return p

    def add_enum(self, text):
        parts = text.split()
        if len(parts) == 2:
            self.enums[parts[1].lower()] = text

    def load_json(self, path):
        j = json.load(open(path))
        for a in j.get("assumptions", []):
            k, t, v = a
            if k == "S":
                m = re.match(r"^sizeof\((.*)\)$", t)
                if m:
                    self.types.set_sizeof(m.group(1), v)
        for a in j.get("assumptions", []):
            k, t, v = a
            if k == "M":
                mt, _, rest = t.partition(" ")
                cls, _, mem = rest.partition("::")
                self.types.members[(cls, mem)] = (self.types.parse(mt), v, t)
        for n in j.get("natives", []):
            self.add_native(n)
        for e in j.get("enums", []):
            self.add_enum(e)
        for m in j.get("members", []):
            mt, _, rest = m.partition(" ")
            cls, _, mem = rest.partition("::")
            self.types.members.setdefault((cls, mem), (self.types.parse(mt), None, m))

    def load_api(self, path):
        """dante_api.json: class method tables -> Dante prototype strings."""
        try:
            j = json.load(open(path))
        except Exception:
            return
        for cls, methods in j.get("classes", {}).items():
            for m in methods:
                proto = m.get("dante") or m.get("script_proto")
                if proto:
                    self.add_native(proto)

    def load_lib(self, path):
        d = DV.load(path)
        for k, off, txt in d.exports:
            if k == "C":
                try:
                    self.add_script(txt)
                except CompileError:
                    pass


# ============================================================ lexer
KEYWORDS = {"if", "else", "while", "for", "do", "return", "break", "continue", "true", "try", "catch",
            "false", "null", "struct", "enum", "native", "extern", "module",
            "thread", "const", "goto"}
PUNCT = ["@==", "@!=", "@=",
         "&&", "||", "==", "!=", "<=", ">=", "->", "+=", "-=", "*=", "/=", "%=", "++", "--", "?",
         "{", "}", "(", ")", "[", "]", ";", ",", ".", "=", "+", "-", "*", "/",
         "%", "<", ">", "!", "@", ":", "&"]


class Tok:
    __slots__ = ("kind", "val", "line")

    def __init__(self, kind, val, line):
        self.kind = kind
        self.val = val
        self.line = line

    def __repr__(self):
        return "%s(%r)" % (self.kind, self.val)


def lex(src):
    toks = []
    i, line = 0, 1
    n = len(src)
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r":
            i += 1
            continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            line += src.count("\n", i, j)
            i = (j + 2) if j >= 0 else n
            continue
        if src.startswith("###BEGIN_DEPENDENCIES", i):
            # An asset manifest for the game's build pipeline, not Dante source.
            # Skip the block but keep the line numbers.
            j = src.find("###END_DEPENDENCIES", i)
            if j < 0:
                err("###BEGIN_DEPENDENCIES without ###END_DEPENDENCIES",
                    Tok("", "", line))
            j += len("###END_DEPENDENCIES")
            line += src.count("\n", i, j)
            i = j
            continue
        if c == '"':
            j = i + 1
            buf = ""
            while j < n and src[j] != '"':
                if src[j] == "\\":
                    nxt = src[j + 1]
                    buf += {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, nxt)
                    j += 2
                else:
                    buf += src[j]
                    j += 1
            toks.append(Tok("str", buf, line))
            i = j + 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            m = re.match(r"\d+\.\d*([eE][-+]?\d+)?f?|\.\d+([eE][-+]?\d+)?f?|\d+[eE][-+]?\d+f?|0[xX][0-9a-fA-F]+|\d+f|\d+", src[i:])
            t = m.group(0)
            if t.endswith("f") and not t.lower().startswith("0x"):
                toks.append(Tok("float", float(t[:-1]), line))
            elif "." in t or "e" in t or "E" in t:
                toks.append(Tok("float", float(t), line))
            elif t.lower().startswith("0x"):
                toks.append(Tok("int", int(t, 16), line))
            else:
                toks.append(Tok("int", int(t), line))
            i += len(t)
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            w = src[i:j]
            wl = w.lower()
            # These three turn up in mixed case (`NULL`, `False`, ...) and are
            # unambiguous reserved words, so fold and use the canonical spelling.
            if w != wl and wl in ("null", "true", "false"):
                toks.append(Tok("kw", wl, line))
            else:
                toks.append(Tok("kw" if w in KEYWORDS else "id", w, line))
            i = j
            continue
        for p in PUNCT:
            if src.startswith(p, i):
                toks.append(Tok("punc", p, line))
                i += len(p)
                break
        else:
            err("unexpected character %r" % c, Tok("", "", line))
    toks.append(Tok("eof", None, line))
    return toks


# ============================================================ AST
class Node:
    def __init__(self, kind, **kw):
        self.kind = kind
        self.__dict__.update(kw)

    def __repr__(self):
        return "%s%r" % (self.kind, {k: v for k, v in self.__dict__.items() if k != "kind"})


class Parser:
    def __init__(self, toks, types):
        self.t = toks
        self.i = 0
        self.types = types

    def peek(self, k=0):
        return self.t[min(self.i + k, len(self.t) - 1)]

    def next(self):
        self.i += 1
        return self.t[self.i - 1]

    def at(self, val, kind=None):
        tk = self.peek()
        return tk.val == val and (kind is None or tk.kind == kind)

    def accept(self, val):
        if self.at(val):
            return self.next()
        return None

    def expect(self, val):
        if not self.at(val):
            err("expected %r, got %r" % (val, self.peek().val), self.peek())
        return self.next()

    # ---- types
    def is_type_start(self):
        tk = self.peek()
        if tk.kind == "punc" and tk.val in ("@", "("):
            if tk.val == "(":
                return False
            return True
        if tk.kind != "id":
            return False
        if tk.val in PRIMS or tk.val.lower() in PRIMS_LOWER:
            return True
        # `SFoo x` / `EFoo x` / `CFoo x` followed by an identifier
        return bool(re.match(r"^[SEC][A-Z]", tk.val)) and self.peek(1).kind == "id"

    def parse_type(self):
        tk = self.peek()
        if tk.kind == "punc" and tk.val == "@":
            ats = ""
            while self.at("@"):
                self.next()
                ats += "@"
            nm = self.next()
            return self.types.parse(ats + str(nm.val))
        if tk.kind == "punc" and tk.val == "(":
            depth, j = 0, self.i
            while True:
                v = self.t[j]
                if v.val == "(":
                    depth += 1
                elif v.val == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            txt = "".join(_tok_text(self.t[k]) for k in range(self.i, j + 1))
            self.i = j + 1
            return self.types.parse(txt)
        self.next()
        txt = str(tk.val)
        return self.types.parse(PRIMS_LOWER.get(txt.lower(), txt))

    # ---- top level
    def parse_module(self):
        decls = []
        while self.peek().kind != "eof":
            decls.append(self.parse_decl())
        return decls

    def parse_decl(self):
        tk = self.peek()
        if tk.val == "module":
            self.next()
            nm = self.next().val
            self.expect(";")
            return Node("module", name=nm)
        if tk.val == "native":
            self.next()
            s = self.next().val
            self.expect(";")
            return Node("native", proto=s)
        if tk.val == "enum":
            self.next()
            ename = self.next().val
            self.expect("{")
            names = []
            while not self.at("}"):
                names.append(self.next().val)
                self.accept(",")
            self.expect("}")
            self.accept(";")
            return Node("enum", name=ename, names=names)
        if tk.val == "struct":
            self.next()
            sname = self.next().val
            self.expect("{")
            fields = []
            while not self.at("}"):
                ft = self.parse_type()
                fn = self.next().val
                self.expect(";")
                fields.append((ft, fn))
            self.expect("}")
            self.accept(";")
            return Node("struct", name=sname, fields=fields)
        ext = False
        if tk.val == "extern":
            self.next()
            if self.peek().kind == "str":
                s = self.next().val
                self.expect(";")
                return Node("externfn", proto=s)
            ext = True
        ty = self.parse_type()
        name = self.next()
        if name.kind not in ("id", "kw"):
            err("expected a name after the type", name)
        if self.at("("):
            self.next()
            params = []
            while not self.at(")"):
                pt = self.parse_type()
                pn = self.next().val
                pdef = self.parse_expr() if self.accept("=") else None
                params.append((pt, pn, pdef))
                if not self.accept(","):
                    break
            self.expect(")")
            if self.accept(";"):
                return Node("protofn", ret=ty, name=name.val, params=params, line=name.line)
            body = self.parse_block()
            return Node("func", ret=ty, name=name.val, params=params, body=body, line=name.line)
        init = None
        if self.accept("="):
            init = self.parse_expr()
        self.expect(";")
        return Node("global", type=ty, name=name.val, init=init, extern=ext, line=name.line)

    # ---- statements
    def parse_block(self):
        self.expect("{")
        body = []
        while not self.at("}"):
            body.append(self.parse_stmt())
        self.expect("}")
        return Node("block", body=body)

    def parse_stmt(self):
        tk = self.peek()
        # `IDENT:` -- a label (function-scoped; see docs/dante_lang.md 2 Statements)
        if tk.kind == "id" and self.peek(1).kind == "punc" and self.peek(1).val == ":":
            self.next()
            self.next()
            return Node("label", name=tk.val, line=tk.line)
        if tk.val == "goto":
            self.next()
            nm = self.next()
            if nm.kind != "id":
                err("goto expects a label name", nm)
            self.expect(";")
            return Node("goto", name=nm.val, line=tk.line)
        if tk.val == "{":
            return self.parse_block()
        if tk.val == ";":
            self.next()
            return Node("block", body=[])
        if tk.val == "if":
            self.next()
            self.expect("(")
            c = self.parse_expr()
            self.expect(")")
            th = self.parse_stmt()
            el = None
            if self.accept("else"):
                el = self.parse_stmt()
            return Node("if", cond=c, then=th, els=el, line=tk.line)
        if tk.val == "while":
            self.next()
            self.expect("(")
            c = self.parse_expr()
            self.expect(")")
            return Node("while", cond=c, body=self.parse_stmt(), line=tk.line)
        if tk.val == "do":
            self.next()
            body = self.parse_stmt()
            self.expect("while")
            self.expect("(")
            c = self.parse_expr()
            self.expect(")")
            self.expect(";")
            return Node("dowhile", cond=c, body=body, line=tk.line)
        if tk.val == "for":
            self.next()
            self.expect("(")
            init = None if self.at(";") else self.parse_simple()
            self.expect(";")
            cond = None if self.at(";") else self.parse_expr()
            self.expect(";")
            step = None if self.at(")") else self.parse_simple()
            self.expect(")")
            return Node("for", init=init, cond=cond, step=step, body=self.parse_stmt(), line=tk.line)
        if tk.val == "return":
            self.next()
            v = None if self.at(";") else self.parse_expr()
            self.expect(";")
            return Node("return", value=v, line=tk.line)
        if tk.val == "break":
            self.next()
            self.expect(";")
            return Node("break", line=tk.line)
        if tk.val == "continue":
            self.next()
            self.expect(";")
            return Node("continue", line=tk.line)
        if tk.val == "thread":
            self.next()
            call = self.parse_expr()
            self.expect(";")
            return Node("thread", call=call, line=tk.line)
        if tk.val == "try":
            self.next()
            body = self.parse_block()
            self.expect("catch")
            handler = self.parse_block()
            return Node("try", body=body, handler=handler, line=tk.line)
        s = self.parse_simple()
        self.expect(";")
        return s

    def parse_simple(self):
        if self.is_type_start():
            ty = self.parse_type()
            name = self.next().val
            init = self.parse_expr() if self.accept("=") else None
            return Node("vardecl", type=ty, name=name, init=init, line=self.peek().line)
        # prefix `++x` / `--x` -- same statement-only sugar as the postfix form below
        if self.peek().kind == "punc" and self.peek().val in ("++", "--"):
            tk = self.next()
            e = self.parse_unary()
            one = Node("int", v=1, line=tk.line)
            return Node("assign", target=e, value=one, op="+" if tk.val == "++" else "-", line=tk.line)
        e = self.parse_expr()
        # `@=` is the dialect's reference-assign; identical to `=` for us, since a
        # value-object destination already gets an implicit LEA.
        if self.at("=") or self.at("@="):
            tk = self.next()
            r = self.parse_expr()
            return Node("assign", target=e, value=r, op=None, line=tk.line)
        if self.peek().kind == "punc" and self.peek().val in ("+=", "-=", "*=", "/=", "%="):
            tk = self.next()
            r = self.parse_expr()
            return Node("assign", target=e, value=r, op=tk.val[0], line=tk.line)
        # postfix `x++` / `x--` -- not a value-producing expression (the VM has no
        # increment opcode); sugar for `x += 1` / `x -= 1` as a statement.
        if self.peek().kind == "punc" and self.peek().val in ("++", "--"):
            tk = self.next()
            one = Node("int", v=1, line=tk.line)
            return Node("assign", target=e, value=one, op="+" if tk.val == "++" else "-", line=tk.line)
        return Node("expr", value=e, line=self.peek().line)

    # ---- expressions (precedence climbing)
    BIN = [("||",), ("&&",), ("==", "!=", "@==", "@!="), ("<", "<=", ">", ">="), ("+", "-"), ("*", "/", "%")]
    REFOP = {"@==": "==", "@!=": "!="}

    def parse_expr(self, lvl=0):
        if lvl >= len(self.BIN):
            return self.parse_unary()
        left = self.parse_expr(lvl + 1)
        while self.peek().kind == "punc" and self.peek().val in self.BIN[lvl]:
            op = self.next()
            right = self.parse_expr(lvl + 1)
            left = Node("bin", op=self.REFOP.get(op.val, op.val), l=left, r=right, line=op.line)
        if lvl == 0 and self.at("?"):
            tk = self.next()
            a = self.parse_expr()
            self.expect(":")
            b = self.parse_expr()
            return Node("ternary", c=left, a=a, b=b, line=tk.line)
        return left

    def parse_unary(self):
        tk = self.peek()
        if tk.kind == "punc" and tk.val in ("-", "!"):
            self.next()
            e = self.parse_unary()
            if tk.val == "-" and e.kind in ("int", "float"):
                # a negative literal is a single immediate (the shipped compiler folds it)
                return Node(e.kind, v=-e.v, line=tk.line)
            return Node("un", op=tk.val, e=e, line=tk.line)
        if tk.kind == "punc" and tk.val == "&":
            self.next()
            return Node("addr", e=self.parse_unary(), line=tk.line)
        if tk.val == "thread":
            self.next()
            return Node("threadx", call=self.parse_postfix(), line=tk.line)
        if tk.kind == "punc" and tk.val == "*":
            self.next()
            return Node("deref", e=self.parse_unary(), line=tk.line)
        if tk.kind == "punc" and tk.val == "(":
            # cast?  `(@CFoo) expr`
            if self.peek(1).val == "@":
                save = self.i
                self.next()
                ty = self.parse_type()
                if self.at(")"):
                    self.next()
                    return Node("cast", type=ty, e=self.parse_unary(), line=tk.line)
                self.i = save
        return self.parse_postfix()

    def parse_postfix(self):
        e = self.parse_atom()
        while True:
            if self.at("."):
                self.next()
                nm = self.next().val
                if self.at("("):
                    self.next()
                    args = self.parse_args()
                    e = Node("mcall", obj=e, name=nm, args=args, line=self.peek().line)
                else:
                    e = Node("member", obj=e, name=nm, line=self.peek().line)
            elif self.at("("):
                self.next()
                args = self.parse_args()
                e = Node("call", fn=e, args=args, line=self.peek().line)
            else:
                return e

    def parse_args(self):
        args = []
        while not self.at(")"):
            args.append(self.parse_expr())
            if not self.accept(","):
                break
        self.expect(")")
        return args

    def parse_atom(self):
        tk = self.next()
        if tk.kind == "int":
            return Node("int", v=tk.val, line=tk.line)
        if tk.kind == "float":
            return Node("float", v=tk.val, line=tk.line)
        if tk.kind == "str":
            return Node("str", v=tk.val, line=tk.line)
        if tk.val == "true":
            return Node("bool", v=True, line=tk.line)
        if tk.val == "false":
            return Node("bool", v=False, line=tk.line)
        if tk.val == "null":
            return Node("null", line=tk.line)
        if tk.val == "(":
            e = self.parse_expr()
            self.expect(")")
            return e
        if tk.kind == "id":
            return Node("id", name=tk.val, line=tk.line)
        err("unexpected token %r" % (tk.val,), tk)


def _tok_text(t):
    if t.kind == "str":
        return '"%s"' % t.val
    return str(t.val)


# ============================================================ code generation
class Place:
    """An addressable location: a stack slot, a global, or a pointer deref."""
    __slots__ = ("type", "mode", "words", "fix", "fix16", "slot", "owned", "defer", "bmark")

    def __init__(self, type, mode, words, fix=None, fix16=None, slot=None):
        self.type = type
        self.mode = mode
        self.words = tuple(words)
        self.fix = list(fix or [])
        self.fix16 = list(fix16 or [])
        self.slot = slot
        self.owned = False       # a String temporary its consumer must STRFREE
        self.defer = None        # a call slot: consumers push their STRFREEs here
        self.bmark = None        # a call result: its block base (released when consumed)

    def operand(self):
        return Operand(self.mode, self.words, list(self.fix), list(self.fix16))


def sl(off, type):
    return Place(type, 3, (off & 0xFFFF,), slot=off)


class Label:
    _n = 0

    def __init__(self, name="L"):
        Label._n += 1
        self.name = "%s%d" % (name, Label._n)
        self.target = None      # Instr or None (end of function)

    def __repr__(self):
        return "@" + self.name


class FuncGen:
    MINFRAME = 32

    def __init__(self, comp, proto, params, name):
        self.c = comp
        self.proto = proto
        self.name = name
        self.code = []
        self.high = 0
        self.locals_end = 8
        self.temp = 8            # arithmetic temporaries (bump, reset per statement)
        self.thigh = 8           # their high-water mark within the statement
        self.bptr = 8            # call blocks / materialisations (stack, above them)
        self.saved = []
        self.scopes = [{}]
        self.dtors = []          # (kind, place-or-slot) stack for scope exit
        self.loops = []
        self.userlabels = {}      # `L_1234:` -> Label   (function-scoped, forward refs ok)
        self.label_defined = {}   # name -> line of its definition
        self.label_refs = {}      # name -> line of the first `goto` that mentions it
        self.epilogue = Label("EPI")
        self.epi_used = False
        # Args descend below fp, the return slot below them.
        sizes = [t.size for t, *_ in params]
        total = sum(sizes)
        acc = 0
        self.params = []
        for i, (t, nm, *_) in enumerate(params):
            acc += sizes[i]
            off = -(total - (acc - sizes[i]))
            self.scopes[0][nm] = sl(off, t)
            self.params.append((t, nm, off))
        self.retplace = None
        if proto.ret.kind != "void":
            self.retplace = sl(-(total + proto.ret.size), proto.ret)

    # -- emission helpers
    def emit(self, op, *ops, **kw):
        ins = Instr(NAMEOP[op], [o if isinstance(o, Operand) else o.operand() for o in ops])
        for o in ins.ops:
            if o.mode in (3, 7):
                v = DV.s16(o.words[0])
                if v > self.high:
                    self.high = v
        if op == "CALL":
            f = DV.s16(ins.ops[1].words[0]) if ins.ops[1].mode == 2 else 0
            if f - 4 > self.high:
                self.high = f - 4
        self.code.append(ins)
        return ins

    def emit_jump(self, op, label, a=None, b=None):
        ins = Instr(NAMEOP[op], [Operand(2, (0,), label=label),
                                 (a.operand() if a is not None else Operand(0)),
                                 (b.operand() if b is not None else Operand(0))])
        for o in ins.ops[1:]:
            if o.mode in (3, 7):
                v = DV.s16(o.words[0])
                if v > self.high:
                    self.high = v
        self.code.append(ins)
        return ins

    def goto(self, label):
        return self.emit_jump("JEQ_I", label, imm_int(0), imm_int(0))

    def user_label(self, name):
        """The `Label` of a source-level `IDENT:` -- created on first mention, so
        a `goto` may name a label defined later in the function."""
        lab = self.userlabels.get(name)
        if lab is None:
            lab = self.userlabels[name] = Label("U_" + name + "_")
        return lab

    def bind(self, label):
        label.target = len(self.code)     # index; resolved after all code is out

    # -- frame allocation, matching the shipped compiler (docs/dante_lang.md 3).

    # run_at runs each expression twice: a dry run sizes the temps, then the real
    # one places call blocks above them.
    def alloc_local(self, size):
        off = self.temp
        self.temp += size
        self.locals_end = self.temp
        self.thigh = self.bptr = self.temp
        return off

    def alloc_temp(self, size):
        off = self.temp
        self.temp += size
        if self.temp > self.thigh:
            self.thigh = self.temp
        if off > self.high:
            self.high = off
        return off

    def reserve(self, size):
        """Reserve `size` bytes that the following operands may reuse."""
        off = self.alloc_temp(size)
        self.temp = off

    def alloc_block(self, size):
        off = self.bptr
        self.bptr += size
        if off > self.high:
            self.high = off
        return off

    def stmt_reset(self, base=None):
        self.temp = self.locals_end if base is None else base
        self.thigh = self.bptr = self.temp

    def begin_dry(self):
        self.saved.append((self.code, self.high, self.temp, self.bptr, self.thigh))
        self.code = []
        self.bptr = self.temp + 0x1000

    def end_dry(self):
        thigh = self.thigh
        self.code, self.high, self.temp, self.bptr, self.thigh = self.saved.pop()
        return thigh

    def run_at(self, base, fn):
        self.stmt_reset(base)
        self.begin_dry()
        try:
            fn()
        finally:
            thigh = self.end_dry()
        self.stmt_reset(base)
        self.bptr = thigh
        fn()

    def push_scope(self):
        self.scopes.append({})
        return (len(self.dtors), self.locals_end)

    def pop_scope(self, mark, emit_dtors=True):
        ndt, lend = mark
        if emit_dtors:
            for kind, place in reversed(self.dtors[ndt:]):
                self.emit("DTORPOP" if kind == "obj" else "STRFREE", place)
        del self.dtors[ndt:]
        self.scopes.pop()
        self.locals_end = lend       # locals are released at scope exit
        self.stmt_reset()

    def lookup(self, name):
        for s in reversed(self.scopes):
            if name in s:
                return s[name]
        return None


def imm_int(v):
    if v == 0:
        return Place(T_INT, 0, ())
    if v == 1:
        return Place(T_INT, 1, ())
    if -0x8000 <= v <= 0x7FFF:
        return Place(T_INT, 2, (v & 0xFFFF,))
    return Place(T_INT, 4, w32(v))


def imm_float(f):
    b = fbits(f)
    if b == 0:
        return Place(T_FLOAT, 0, ())
    return Place(T_FLOAT, 4, w32(b))


def imm_bool(b):
    return Place(T_BOOL, 1 if b else 0, ())


NULL = Place(Type("@CActor", "obj", 4), 0, ())
BUILTINS = ("toInt", "toFloat", "toString", "dot")

ARITH = {
    ("int", "+"): "ADDI", ("int", "-"): "SUBI", ("int", "*"): "MULI",
    ("int", "/"): "DIVI", ("int", "%"): "MODI",
    ("float", "+"): "ADDF", ("float", "-"): "SUBF", ("float", "*"): "MULF",
    ("float", "/"): "DIVF", ("float", "%"): "MODF",
    ("Vector", "+"): "ADDV", ("Vector", "-"): "SUBV",
    ("String", "+"): "STRCAT",
}
CMPSUF = {"int": "I", "float": "F", "bool": "B", "String": "S", "Vector": "V",
          "obj": "I", "ptr": "I", "enum": "I", "func": "I", "opaque": "I", "struct": "I"}
JMPFOR = {"<": "JLT", "<=": "JLE", "==": "JEQ", "!=": "JNE"}
SETFOR = {"<": "LT", "<=": "LE", "==": "EQ", "!=": "NE"}
NEGATE = {"<": ">=", ">=": "<", ">": "<=", "<=": ">", "==": "!=", "!=": "=="}


class Compiler:
    def __init__(self, db, types, module="mod"):
        self.db = db
        self.types = types
        self.module = module
        self.strings = {}          # text -> data offset
        self.strorder = []
        self.globals = {}          # name -> (Type, symbol text, own?)
        self.globinit = []         # (Place, Node) for __<stem>_init
        self.funcs = []            # (Proto, Node)
        self.used_assume = {}      # text -> (kind, value)
        self.code_out = []
        self.data_size = 0

    # ---------------- data
    def string_ref(self, text):
        if text not in self.strings:
            off = 4 * len(self.strorder)
            self.strings[text] = off
            self.strorder.append(text)
        off = self.strings[text]
        return Place(T_STRING, 5, w32(off), fix=[("D", "__%s_data" % self.module)])

    def global_place(self, name):
        # Case-insensitive: one actor spelled several ways is still one global, and
        # its own declared casing is what reaches the fixup.
        g = self.globals.get(name.lower())
        if not g:
            return None
        t, sym, own = g
        return Place(t, 5, (0, 0), fix=[("D", sym)])

    def use_assume(self, text, kind, value):
        if value is None:
            return
        self.used_assume[text] = (kind, value)

    def member_of(self, stype, name, tok=None):
        info = self.types.member(stype, name)
        if not info:
            err("unknown member %s::%s" % (stype, name), tok)
        mt, off, text = info
        if off is None:
            err("member %s::%s has no known offset (add it to the symbol DB)" % (stype, name), tok)
        self.use_assume(text, "M", off)
        return mt, off, text

    # ---------------- declarations
    def collect(self, decls):
        for d in decls:
            if d.kind == "module":
                self.module = d.name
            elif d.kind == "native":
                self.db.add_native(d.proto, declared=True)
            elif d.kind == "externfn":
                self.db.add_script(d.proto)
            elif d.kind == "enum":
                for nm in d.names:
                    self.db.enums[nm.lower()] = "%s %s" % (d.name, nm)
            elif d.kind == "struct":
                off = 0
                for ft, fn in d.fields:
                    text = "%s %s::%s" % (ft.txt, d.name, fn)
                    self.types.members[(d.name, fn)] = (ft, off, text)
                    off += ft.size
                self.types.set_sizeof(d.name, off)
            elif d.kind == "protofn":
                p = self.db.add_script(proto_text(d.ret, d.name, [t for t, _, _ in d.params]))
                attach_defaults(p, d.params)
        # globals then functions (so functions can see all globals)
        for d in decls:
            if d.kind == "global":
                sym = "%s %s" % (d.type.txt, d.name)
                self.globals[d.name.lower()] = (d.type, sym, not d.extern)
        for d in decls:
            if d.kind == "func":
                p = self.db.add_script(proto_text(d.ret, d.name, [t for t, _, _ in d.params]))
                attach_defaults(p, d.params)
                self.funcs.append((p, d))

    # ---------------- compile
    def compile(self, decls, emit_init=True):
        self.collect(decls)
        mods = []
        for p, d in self.funcs:
            mods.append(self.gen_func(p, d))
        if emit_init:
            mods.append(self.gen_init([d for d in decls if d.kind == "global" and not d.extern]))
        return mods

    def gen_init(self, gdecls):
        gd = [d for d in gdecls if d.init is not None or d.type.kind == "struct"]
        proto = Proto("void __%s_init()" % self.module, self.types)
        f = FuncGen(self, proto, [], "__%s_init" % self.module)
        for d in gdecls:
            place = self.global_place(d.name)
            t = d.type
            if t.kind == "String":
                f.emit("STRNEW", place, imm_int(0))
            if t.kind in ("cls", "struct") and d.init is None:
                # an engine-owned object: construct it and register its destructor
                # on the program-static list (mode-5 CTOR), as the shipped init does
                ctor = self.find_native(t.txt, "constructor", 0, required=False)
                dtor = self.find_native(t.txt, "destructor", 0, required=False)
                if ctor and dtor:
                    f.stmt_reset()
                    off = f.alloc_block(4)
                    f.emit("LEA", sl(off, t), place)
                    f.emit("CALL", Operand(4, (0, 0), fix=[("N", ctor.text)]),
                           Operand(2, ((off + 4) & 0xFFFF,)), Operand(2, (off & 0xFFFF,)))
                    f.emit("CTOR", place, Operand(0), Operand(4, (0, 0), fix=[("N", dtor.text)]))
                continue
            if d.init is not None:
                f.stmt_reset()
                v = None
                if d.init.kind == "id" and t.kind in ("obj", "ptr"):
                    g = self.global_place(d.init.name)
                    if g is not None and g.type.kind in ("cls", "struct"):
                        f.emit("LEA", place, g)      # `@CFoo r = valueObject;` is a direct LEA here
                        continue
                self.gen_store(f, place, d.init)
        f.emit("RET")
        return (proto, f)

    def gen_func(self, proto, d):
        f = FuncGen(self, proto, d.params, d.name)
        mark = (0, 8)
        self.gen_stmt(f, d.body)
        for nm, line in sorted(f.label_refs.items(), key=lambda kv: kv[1]):
            if nm not in f.label_defined:
                raise CompileError("line %d: goto to undefined label %s in %s"
                                   % (line, nm, d.name))
        ft = falls_through(d.body)
        if ft:
            for kind, place in reversed(f.dtors):
                f.emit("DTORPOP" if kind == "obj" else "STRFREE", place)
        if ft or f.epi_used:
            f.bind(f.epilogue)
            f.emit("RET")            # omitted after an infinite loop (unreachable)
        return (proto, f)

    # ---------------- statements
    def gen_stmt(self, f, s, base=None):
        k = s.kind
        if k == "block":
            mark = f.push_scope()
            for st in s.body:
                self.gen_stmt(f, st)
            f.pop_scope(mark, emit_dtors=falls_through(s))   # no dead destructors
        elif k == "vardecl":
            f.stmt_reset(base)
            self.gen_vardecl(f, s)
        elif k == "assign":
            f.stmt_reset(base)
            if s.op is None:
                f.run_at(f.temp, lambda: self.gen_store(f, self.gen_lvalue(f, s.target), s.value,
                                                        assign=True))
            else:
                f.run_at(f.temp, lambda: self.gen_compound(f, s))
        elif k == "expr":
            f.stmt_reset(base)
            f.run_at(f.temp, lambda: self.release(f, self.gen_expr(f, s.value, want=None)))
        elif k == "return":
            f.stmt_reset(base)
            if s.value is not None:
                if f.retplace is None:
                    err("return with a value in a void function", s)
                f.run_at(f.temp, lambda: self.gen_store(
                    f, f.retplace, s.value, assign=(f.retplace.type.kind == "String")))
            for kind, place in reversed(f.dtors):
                f.emit("DTORPOP" if kind == "obj" else "STRFREE", place)
            f.epi_used = True
            f.goto(f.epilogue)
        elif k == "if":
            # `if (cond) goto L;` is one conditional jump, exactly as the shipped
            # compiler writes it -- no inverted branch around an unconditional goto
            g = _only_goto(s)
            if g is not None:
                f.stmt_reset(base)
                f.label_refs.setdefault(g.name, g.line)
                f.run_at(f.temp, lambda: self.gen_cond(
                    f, s.cond, true_label=f.user_label(g.name)))
                return
            els = Label("ELSE")
            end = Label("ENDIF")
            f.stmt_reset(base)
            f.run_at(f.temp, lambda: self.gen_cond(
                f, s.cond, false_label=(els if s.els else end)))
            self.gen_stmt(f, s.then)
            if s.els:
                if falls_through(s.then):
                    f.goto(end)
                f.bind(els)
                self.gen_stmt(f, s.els)
            f.bind(end)
        elif k == "while":
            top = Label("WTOP")
            cond = Label("WCOND")
            end = Label("WEND")
            mark = f.push_scope()
            f.goto(cond)
            f.bind(top)
            f.loops.append((cond, end, len(f.dtors)))
            self.gen_body(f, s.body)
            f.loops.pop()
            f.bind(cond)
            f.run_at(f.locals_end, lambda: self.gen_cond(f, s.cond, true_label=top))
            f.bind(end)
            f.pop_scope(mark)
        elif k == "dowhile":
            # `top: body; cond -> top` -- no entry jump
            top = Label("DTOP")
            cond = Label("DCOND")
            end = Label("DEND")
            mark = f.push_scope()
            f.bind(top)
            f.loops.append((cond, end, len(f.dtors)))
            self.gen_body(f, s.body)
            f.loops.pop()
            f.bind(cond)
            f.run_at(f.locals_end, lambda: self.gen_cond(f, s.cond, true_label=top))
            f.bind(end)
            f.pop_scope(mark)
        elif k == "for":
            mark = f.push_scope()
            if s.init is not None:
                self.gen_stmt(f, s.init)
            top = Label("FTOP")
            cont = Label("FCONT")
            cond = Label("FCOND")
            end = Label("FEND")
            if s.cond is not None:
                f.goto(cond)
            f.bind(top)
            f.loops.append((cont, end, len(f.dtors)))
            self.gen_body(f, s.body)
            f.loops.pop()
            f.bind(cont)
            if s.step is not None:
                self.gen_stmt(f, s.step)
            f.bind(cond)
            if s.cond is None:
                f.goto(top)
            else:
                f.run_at(f.locals_end, lambda: self.gen_cond(f, s.cond, true_label=top))
            f.bind(end)
            f.pop_scope(mark)
        elif k == "label":
            if s.name in f.label_defined:
                err("duplicate label %s (first defined on line %d)"
                    % (s.name, f.label_defined[s.name]), s)
            f.label_defined[s.name] = s.line
            f.bind(f.user_label(s.name))
            f.stmt_reset()
        elif k == "goto":
            f.label_refs.setdefault(s.name, s.line)
            f.goto(f.user_label(s.name))
        elif k in ("break", "continue"):
            if not f.loops:
                err("%s outside a loop" % k, s)
            cont, end, ndt = f.loops[-1]
            for kind, place in reversed(f.dtors[ndt:]):     # leave the loop's inner scopes
                f.emit("DTORPOP" if kind == "obj" else "STRFREE", place)
            f.goto(end if k == "break" else cont)
        elif k == "thread":
            f.stmt_reset(base)
            f.run_at(f.temp, lambda: self.gen_thread(f, s.call))
        elif k == "try":
            # TRY -> handler; body; ENDTRY; goto end; handler: ...; end:
            # The engine unwinds here when a thread is aborted, e.g. a skipped cinematic.
            catch = Label("CATCH")
            end = Label("ENDTRY")
            f.code.append(Instr(NAMEOP["TRY"], [Operand(2, (0,), label=catch)]))
            self.gen_stmt(f, s.body)
            f.emit("ENDTRY", Operand(2, (4,)))
            if falls_through(s.body):
                f.goto(end)
            f.bind(catch)
            self.gen_stmt(f, s.handler)
            f.bind(end)
        else:
            err("cannot compile statement %r" % k, s)

    def gen_body(self, f, s):
        """A loop body is an ordinary scope: its locals are released before the
        condition and step (generated after the body) run."""
        self.gen_stmt(f, s)

    def gen_compound(self, f, s):
        """`x op= e`: the lvalue is evaluated once (one pointer materialisation)."""
        place = self.gen_lvalue(f, s.target)
        node = Node("bin", op=s.op, l=s.target, r=s.value, line=s.line)
        node.lplace = place
        v = self.gen_expr(f, node, want=place.type, dest=place)
        if v is not place:
            err("cannot compile compound assignment", s)

    def gen_vardecl(self, f, s):
        t = s.type
        off = f.alloc_local(t.size)
        place = sl(off, t)
        f.scopes[-1][s.name] = place
        if t.kind == "cls" and t.txt not in self.types.sizeof and s.init is None:
            # A bare `CFoo x;` needs real storage and we don't know CFoo's layout;
            # 4 bytes and no ctor would let a native write over the frame.
            err("unknown class %s declared by value (no sizeof in the symbol DB;"
                " a reference is @%s)" % (t.txt, t.txt), s)
        if t.kind == "struct" or (t.kind == "cls" and t.txt in self.types.sizeof):
            sz = self.types.sizeof.get(t.txt)
            if sz is None:
                err("unknown struct %s (no sizeof in the symbol DB)" % t.txt, s)
            self.use_assume("sizeof(%s)" % t.txt, "S", sz)
            # engine structs have a registered constructor/destructor pair; a struct
            # declared in the source is plain storage and gets neither
            ctor = self.find_native(t.txt, "constructor", 0, required=False)
            dtor = self.find_native(t.txt, "destructor", 0, required=False)
            if ctor and dtor:
                f.emit("LEA", place, place)
                f.emit("CALL", Operand(4, (0, 0), fix=[("N", ctor.text)]),
                       Operand(2, ((off + 4) & 0xFFFF,)), Operand(2, (off & 0xFFFF,)))
                f.emit("CTOR", place, Operand(0), Operand(4, (0, 0), fix=[("N", dtor.text)]))
                f.dtors.append(("obj", place))
            f.stmt_reset()
        elif t.kind == "String":
            f.emit("STRNEW", place, imm_int(0))
            f.dtors.append(("str", place))
            f.stmt_reset()
        if s.init is not None:
            f.stmt_reset()
            f.run_at(f.temp, lambda: self.gen_store(f, place, s.init))

    # ---------------- assignment
    def is_builtin_call(self, e):
        return e.kind == "call" and e.fn.kind == "id" and e.fn.name in BUILTINS

    def is_direct(self, e, t, place=None):
        """Does the shipped compiler write this straight into its destination?"""
        if e.kind in ("bin", "un", "ternary") or self.is_builtin_call(e):
            return True
        if e.kind == "cast" and t.kind in ("obj", "cls"):
            return place is None or place.mode == 3
        return False


    def gen_store(self, f, place, expr, assign=False):
        """Store `expr` into `place`.  A plain `x = e;` always goes via a temporary."""
        t = place.type
        direct = self.is_direct(expr, t, place) and not assign
        v = self.gen_expr(f, expr, want=t, dest=(place if direct else None))
        if v is place:
            return
        if t.kind == "String":
            f.emit("STRCPY", place, v)
            self.release(f, v)
        elif t.kind == "Vector":
            f.emit("MOVV", place, v)
        else:
            v = self.as_ref(f, v, t, dest=(place if place.mode == 3 and not assign else None))
            if v is place:
                return
            if t.kind == "float" and v.type.kind == "int":
                f.emit("ITOF", place, v)
            else:
                f.emit("MOV", place, v)
        self.consume(f, v)

    def as_ref(self, f, v, t, dest=None):
        """A value object (`CFoo` global, struct local) used where a reference is
        wanted: the shipped compiler takes its address (`LEA`)."""
        if t is not None and t.kind in ("obj", "ptr") and v.type.kind in ("cls", "struct"):
            d = dest if dest is not None else sl(f.alloc_block(4), t)
            f.emit("LEA", d, v)
            return d
        return v

    def consume(self, f, v):
        """A call result has been used: pop its block if it is on top."""
        bm = getattr(v, "bmark", None)
        if bm is not None and f.bptr == bm + (v.type.size or 4):
            f.bptr = bm

    def release(self, f, v, dest=None):
        """Free a String temporary after its consumer used it; a consumer that wrote
        into a call slot defers the free to the call's cleanup."""
        if not getattr(v, "owned", False):
            return
        if dest is not None and dest.defer is not None:
            dest.defer.append(v)
        else:
            f.emit("STRFREE", v)

    def coerce(self, f, v, t, node=None):
        if t is None or v.type.kind == t.kind:
            return v
        if t.kind == "float" and v.type.kind == "int":
            off = f.alloc_temp(4)
            d = sl(off, T_FLOAT)
            f.emit("ITOF", d, v)
            return d
        return v

    # ---------------- lvalues
    def gen_lvalue(self, f, e):
        if e.kind == "id":
            p = f.lookup(e.name)
            if p is not None:
                return p
            g = self.global_place(e.name)
            if g is not None:
                return g
            err("unknown variable %r" % e.name, e)
        if e.kind == "member":
            return self.gen_member(f, e)
        if e.kind == "deref":
            return self.gen_deref(f, e)
        err("not an assignable expression (%s)" % e.kind, e)

    def gen_deref(self, f, e):
        v = self.gen_expr(f, e.e, want=None)
        if v.type.kind != "ptr":
            err("cannot dereference %s" % v.type.txt, e)
        inner = self.types.parse(v.type.inner)
        return Place(inner, 7, (self.materialize(f, v) & 0xFFFF, 0))

    def gen_member(self, f, e):
        base = self.gen_expr(f, e.obj, want=None)
        bt = base.type
        if bt.kind in ("struct", "cls"):
            mt, off, _ = self.member_of(bt.txt, e.name, e)
            if base.mode == 3:
                return sl(DV.s16(base.words[0]) + off, mt)
            if base.mode == 5:
                if bt.kind == "struct":
                    # a struct global's member: the offset is baked into the words
                    # (guarded by the ASSUMPTIONS line), only the D fixup remains
                    return Place(mt, 5, w32(off), fix=list(base.fix), fix16=list(base.fix16))
                p = Place(mt, 5, base.words, fix=list(base.fix), fix16=list(base.fix16))
                p.fix.append(("M", self.member_text(bt.txt, e.name)))
                return p
            err("unsupported struct member base", e)
        if bt.kind in ("ptr", "obj"):
            inner = bt.inner
            mt, off, text = self.member_of(inner, e.name, e)
            slot = self.materialize(f, base)
            if re.match(r"^S[A-Z]", inner or ""):
                # struct members through a pointer: the offset is baked (the
                # ASSUMPTIONS line guards it); class members get an M fixup
                return Place(mt, 7, (slot & 0xFFFF, off & 0xFFFF))
            return Place(mt, 7, (slot & 0xFFFF, 0), fix16=[("M", text)])
        if bt.kind == "Vector":
            off = {"x": 0, "y": 4, "z": 8}.get(e.name)
            if off is None:
                err("Vector has no member %r" % e.name, e)
            self.use_assume("float Vector::%s" % e.name, "M", off)
            if base.mode == 3:
                return sl(DV.s16(base.words[0]) + off, T_FLOAT)
            if base.mode == 5:
                p = Place(T_FLOAT, 5, base.words, fix=list(base.fix))
                p.fix.append(("M", "float Vector::%s" % e.name))
                return p
        err("cannot take member %r of %s" % (e.name, bt.txt), e)

    def member_text(self, cls, name):
        info = self.types.member(cls, name)
        return info[2]

    def materialize(self, f, v):
        """Force a value into a stack slot and return its offset."""
        if v.mode == 3:
            return DV.s16(v.words[0])
        off = f.alloc_block(v.type.size or 4)
        d = sl(off, v.type)
        f.emit("MOVV" if v.type.kind == "Vector" else "MOV", d, v)
        return off

    # ---------------- expressions
    def gen_expr(self, f, e, want=None, dest=None):
        k = e.kind
        if k == "int":
            if want is not None and want.kind == "float":
                return imm_float(float(e.v))
            return imm_int(e.v)
        if k == "float":
            return imm_float(e.v)
        if k == "bool":
            return imm_bool(e.v)
        if k == "null":
            return NULL
        if k == "str":
            return self.string_ref(e.v)
        if k == "id":
            p = f.lookup(e.name)
            if p is not None:
                return p
            g = self.global_place(e.name)
            if g is not None:
                return g
            if e.name.lower() in self.db.enums:
                enumtext = self.db.enums[e.name.lower()]
                return Place(self.types.parse(enumtext.split()[0]), 4, (0, 0),
                             fix=[("I", enumtext)])
            fns = self.db.scriptfns.get(e.name)
            if fns and len(fns) == 1:
                p = fns[0]
                ft = self.types.parse("(%s(%s))" % (p.ret.txt, ",".join(x.txt for x in p.params)))
                return Place(ft, 4, (0, 0), fix=[("C", p.text)])
            err("unknown identifier %r" % e.name, e)
        if k == "deref":
            return self.gen_deref(f, e)
        if k == "member":
            obj = e.obj
            if obj.kind == "id" and obj.name.lower() not in self.db.enums and f.lookup(obj.name) is None \
                    and self.global_place(obj.name) is None and re.match(r"^E[A-Z]", obj.name):
                sym = "%s %s" % (obj.name, e.name)
                return Place(self.types.parse(obj.name), 4, (0, 0), fix=[("I", sym)])
            return self.gen_member(f, e)
        if k == "cast":
            if e.type.kind in ("obj", "cls"):
                mark = f.temp
                d = dest if dest is not None else sl(f.alloc_temp(4), e.type)
                v = self.gen_expr(f, e.e, want=None)
                src = v if v.mode in (3, 5, 7) else None
                if src is None:
                    src = sl(self.materialize(f, v), v.type)
                f.emit("DYNCAST", d, src, Operand(4, w32(classid(e.type.inner))))
                self.consume(f, v)
                f.temp = mark + (4 if dest is None else 0)
                return d
            v = self.gen_expr(f, e.e, want=None)
            return Place(e.type, v.mode, v.words, v.fix, v.fix16, v.slot)
        if k == "addr":
            bt = self.static_type_node(f, e.e)
            pt = self.types.parse("@" + (bt.txt if bt is not None else "int"))
            d = dest if dest is not None else sl(f.alloc_block(4), pt)
            v = self.gen_lvalue(f, e.e)
            f.emit("LEA", d, v)
            return d
        if k == "un":
            if e.op == "-":
                st = self.static_type(f, e.e)
                vt = {"float": T_FLOAT, "Vector": T_VEC}.get(st, T_INT)
                if want is not None and want.kind == "float" and st == "int":
                    vt = T_FLOAT
                mark = f.temp
                d = dest if dest is not None else sl(f.alloc_temp(vt.size), vt)
                v = self.gen_expr(f, e.e, want=want)
                f.temp = mark + (vt.size if dest is None else 0)
                if v.type.kind == "float":
                    f.emit("SUBF", d, imm_float(0.0), v)
                elif v.type.kind == "Vector":
                    f.emit("SUBV", d, imm_int(0), v)
                else:
                    f.emit("SUBI", d, imm_int(0), v)
                return d
            if e.op == "!":
                mark = f.temp
                d = dest if dest is not None else sl(f.alloc_temp(4), T_BOOL)
                v = self.gen_expr(f, e.e, want=T_BOOL)
                f.emit("MOV", d, v)
                f.emit("EQ_B", d, d, imm_int(0))
                self.consume(f, v)
                f.temp = mark + (4 if dest is None else 0)
                return d
        if k == "bin":
            return self.gen_bin(f, e, want, dest)
        if k == "ternary":
            # `c ? a : b`: both arms are stored straight into the destination
            t = want if (dest is None and want is not None) else None
            if dest is None:
                t = self.static_type_node(f, e.a) or want or T_INT
                dest = sl(f.alloc_temp(t.size or 4), t)
            els = Label("TELSE")
            end = Label("TEND")
            self.gen_cond(f, e.c, false_label=els)
            self.gen_store(f, dest, e.a)
            f.goto(end)
            f.bind(els)
            self.gen_store(f, dest, e.b)
            f.bind(end)
            return dest
        if k == "call":
            return self.gen_call(f, e, want, dest)
        if k == "threadx":
            return self.gen_thread(f, e.call)
        if k == "mcall":
            return self.gen_mcall(f, e, want)
        err("cannot compile expression %r" % k, e)

    def gen_bin(self, f, e, want, dest=None):
        op = e.op
        if op in ("&&", "||"):
            d = dest if dest is not None else sl(f.alloc_temp(4), T_BOOL)
            t = Label("BT")
            fa = Label("BF")
            end = Label("BE")
            self.gen_cond(f, e, false_label=fa)
            f.emit("MOV", d, imm_int(1))
            f.goto(end)
            f.bind(fa)
            f.emit("MOV", d, imm_int(0))
            f.bind(end)
            return d
        if op in ("==", "!=", "<", "<=", ">", ">="):
            mark = f.temp
            d = dest if dest is not None else sl(f.alloc_temp(4), T_BOOL)
            l, r, op2 = self.cmp_operands(f, e, op)
            suf = CMPSUF[l.type.kind]
            name = SETFOR[op2] + "_" + suf
            if name not in NAMEOP:
                name = SETFOR[op2] + "_I"
            f.emit(name, d, l, r)
            self.consume(f, r)
            self.consume(f, l)
            f.temp = mark + (4 if dest is None else 0)
            return d
        # arithmetic
        lt = self.static_type(f, e.l)
        rt = self.static_type(f, e.r)
        if "Vector" in (lt, rt):
            kind = "Vector"
        elif "String" in (lt, rt):
            kind = "String"
        elif "float" in (lt, rt):
            kind = "float"
        else:
            kind = "int"
        if kind == "Vector" and op in ("*", "/"):
            # the VM only has vector-by-scalar: MULVS/DIVVS take the scalar in src2
            ln, rn = (e.l, e.r) if lt == "Vector" else (e.r, e.l)
            if op == "/" and lt != "Vector":
                err("cannot divide a scalar by a Vector", e)
            mark = f.temp
            d = dest if dest is not None else sl(f.alloc_temp(12), T_VEC)
            lp = getattr(e, "lplace", None)
            v = lp if lp is not None else self.gen_expr(f, ln, want=T_VEC)
            s = self.gen_num(f, rn, "float")
            f.emit("MULVS" if op == "*" else "DIVVS", d, v, s)
            self.consume(f, s)
            self.consume(f, v)
            f.temp = mark + (12 if dest is None else 0)
            return d
        want2 = {"float": T_FLOAT, "int": T_INT, "String": T_STRING, "Vector": T_VEC}[kind]
        mn = ARITH.get((kind, op))
        if mn is None:
            err("operator %r is not defined for %s" % (op, kind), e)
        mark = f.temp
        d = dest if dest is not None else sl(f.alloc_temp(want2.size), want2)
        if kind == "String" and dest is None:
            f.emit("STRNEW", d, imm_int(0))
            d.owned = True
        lp = getattr(e, "lplace", None)
        l = lp if lp is not None else self.gen_num(f, e.l, kind, want2)
        r = self.gen_num(f, e.r, kind, want2)
        f.emit(mn, d, l, r)
        if kind == "String":
            # the right operand is freed first; a deferred free list runs reversed,
            # so it is pushed left then right
            if dest is not None and dest.defer is not None:
                self.release(f, l, dest)
                self.release(f, r, dest)
            else:
                self.release(f, r, dest)
                self.release(f, l, dest)
        self.consume(f, r)
        self.consume(f, l)
        f.temp = mark + (want2.size if dest is None else 0)
        return d

    def gen_num(self, f, e, fam, want=None):
        """An operand of `fam` arithmetic, allocating any ITOF temporary pre-order."""
        if fam == "float":
            if e.kind not in ("int", "float") and self.static_type(f, e) == "int":
                off = f.alloc_temp(4)
                v = self.gen_expr(f, e, want=T_FLOAT)
                if v.type.kind == "int":
                    d = sl(off, T_FLOAT)
                    f.emit("ITOF", d, v)
                    return d
                return v
            return self.coerce(f, self.gen_expr(f, e, want=T_FLOAT), T_FLOAT, e)
        return self.gen_expr(f, e, want=(T_INT if fam == "int" else want))

    def cmp_operands(self, f, e, op):
        ln, rn = (e.r, e.l) if op in (">", ">=") else (e.l, e.r)
        op2 = {">": "<", ">=": "<="}.get(op, op)
        lt, rt = self.static_type(f, ln), self.static_type(f, rn)
        fam = "float" if "float" in (lt, rt) else None
        l = self.gen_num(f, ln, fam) if fam else self.gen_expr(f, ln, want=None)
        r = self.gen_num(f, rn, fam) if fam else self.gen_expr(f, rn, want=self.hint(l))
        if l.type.kind == "float" and r.type.kind == "int":
            r = self.coerce(f, r, T_FLOAT)
        if r.type.kind == "float" and l.type.kind == "int":
            l = self.coerce(f, l, T_FLOAT)
        l = self.as_ref(f, l, r.type)
        r = self.as_ref(f, r, l.type)
        return l, r, op2

    def hint(self, place):
        return place.type

    def static_type(self, f, e):
        """A cheap type guess used to pick int/float/String/Vector arithmetic."""
        if e.kind == "float":
            return "float"
        if e.kind == "int":
            return "int"
        if e.kind == "str":
            return "String"
        if e.kind == "bool":
            return "bool"
        if e.kind == "id":
            p = f.lookup(e.name)
            if p is None:
                g = self.global_place(e.name)
                p = g
            if p is not None:
                return p.type.kind
            return "int"
        if e.kind == "bin":
            a, b = self.static_type(f, e.l), self.static_type(f, e.r)
            for pref in ("Vector", "String", "float"):
                if pref in (a, b):
                    return pref
            return a
        if e.kind == "un":
            return self.static_type(f, e.e)
        if e.kind == "ternary":
            return self.static_type(f, e.a)
        if e.kind == "addr":
            bt = self.static_type_node(f, e)
            return bt.kind if bt else "obj"
        if e.kind == "cast":
            return e.type.kind
        if e.kind == "deref":
            bt = self.static_type_node(f, e.e)
            return self.types.parse(bt.inner).kind if (bt and bt.kind == "ptr") else "int"
        if e.kind == "member":
            try:
                bt = self.static_type_node(f, e.obj)
                if bt and bt.kind == "Vector":
                    return "float"
                if bt and bt.kind in ("struct", "ptr", "obj"):
                    nm = bt.txt if bt.kind == "struct" else bt.inner
                    info = self.types.member(nm, e.name)
                    if info:
                        return info[0].kind
            except CompileError:
                pass
            return "int"
        if e.kind in ("call", "mcall"):
            p = self.resolve_call(f, e)
            return p.ret.kind if p else "int"
        return "int"

    def static_type_node(self, f, e):
        if e.kind in ("bin", "un"):
            fam = self.static_type(f, e)
            return {"Vector": T_VEC, "float": T_FLOAT, "int": T_INT, "String": T_STRING,
                    "bool": T_BOOL}.get(fam)
        if e.kind == "ternary":
            return self.static_type_node(f, e.a)
        if e.kind == "addr":
            bt = self.static_type_node(f, e.e)
            return self.types.parse("@" + bt.txt) if bt else None
        if e.kind == "id":
            p = f.lookup(e.name) or self.global_place(e.name)
            return p.type if p else None
        if e.kind == "cast":
            return e.type
        if e.kind == "deref":
            bt = self.static_type_node(f, e.e)
            return self.types.parse(bt.inner) if (bt and bt.kind == "ptr") else None
        if e.kind == "member":
            bt = self.static_type_node(f, e.obj)
            if bt is None:
                return None
            nm = bt.txt if bt.kind == "struct" else (bt.inner if bt.kind in ("ptr", "obj") else None)
            if nm:
                info = self.types.member(nm, e.name)
                return info[0] if info else None
        if e.kind in ("call", "mcall"):
            p = self.resolve_call(f, e)
            return p.ret if p else None
        return None

    # ---------------- conditions
    def gen_cond(self, f, e, true_label=None, false_label=None):
        """Emit branches so control reaches `true_label` when e is true (and
        falls through otherwise), or `false_label` when it is false."""
        if e.kind == "bin" and e.op == "&&":
            if false_label is not None:
                self.gen_cond(f, e.l, false_label=false_label)
                self.gen_cond(f, e.r, false_label=false_label)
            else:
                skip = Label("AND")
                self.gen_cond(f, e.l, false_label=skip)
                self.gen_cond(f, e.r, true_label=true_label)
                f.bind(skip)
            return
        if e.kind == "bin" and e.op == "||":
            if true_label is not None:
                self.gen_cond(f, e.l, true_label=true_label)
                self.gen_cond(f, e.r, true_label=true_label)
            else:
                hit = Label("OR")
                self.gen_cond(f, e.l, true_label=hit)
                self.gen_cond(f, e.r, false_label=false_label)
                f.bind(hit)
            return
        if e.kind == "un" and e.op == "!":
            return self.gen_cond(f, e.e, true_label=false_label, false_label=true_label)
        if e.kind == "bin" and (e.op in JMPFOR or e.op in (">", ">=")):
            op = e.op
            if false_label is not None:
                op = NEGATE[op]
                lab = false_label
            else:
                lab = true_label
            node = Node("bin", op=op, l=e.l, r=e.r, line=getattr(e, "line", 0))
            # A branch comparison holds one block slot while its operands evaluate,
            # then releases it, so the next && / || operand starts back at the base.
            mark = f.bptr
            tmark = f.temp
            f.alloc_block(4)
            tmp = None
            if op in ("==", "!=") and self.static_type(f, e.l) == "String" or \
                    self.static_type(f, e.r) == "String":
                tmp = sl(f.alloc_temp(4), T_BOOL)      # the String comparison's result
            l, r, op2 = self.cmp_operands(f, node, op)
            suf = CMPSUF[l.type.kind]
            if suf == "S" and (getattr(l, "owned", False) or getattr(r, "owned", False)):
                # a String temporary must be freed before the branch: compute the
                # comparison into a bool, free, then branch on the bool
                f.emit(SETFOR[op2] + "_S", tmp, l, r)
                self.release(f, r)
                self.release(f, l)
                self.consume(f, r)
                self.consume(f, l)
                f.emit_jump("JEQ_B" if false_label is not None else "JNE_B", lab, tmp, imm_int(0))
                f.bptr = mark
                f.temp = tmark
                return
            name = JMPFOR[op2] + "_" + suf
            if name not in NAMEOP:
                name = JMPFOR[op2] + "_I"
            f.emit_jump(name, lab, l, r)
            self.consume(f, r)
            self.consume(f, l)
            f.bptr = mark
            f.temp = tmark
            return
        # generic truth test
        v = self.gen_expr(f, e, want=T_BOOL)
        suf = CMPSUF.get(v.type.kind, "I")
        if false_label is not None:
            f.emit_jump("JEQ_" + suf if ("JEQ_" + suf) in NAMEOP else "JEQ_I", false_label, v, imm_int(0))
        else:
            f.emit_jump("JNE_" + suf if ("JNE_" + suf) in NAMEOP else "JNE_I", true_label, v, imm_int(0))
        self.consume(f, v)

    # ---------------- calls
    def _best_arity(self, cands, n):
        """Pick an overload: exact arity wins, default-fillable only if nothing does."""
        exact = [p for p in cands if len(p.params) == n]
        if exact:
            return exact[0]
        fillable = [p for p in cands if params_match(p, n)]
        return fillable[0] if fillable else None

    def resolve_call(self, f, e):
        if e.kind == "call":
            if e.fn.kind != "id":
                return None
            name = e.fn.name
            n = len(e.args)
            p = self._best_arity(self.db.by_name.get((None, name), []), n)
            if p:
                return p
            return self._best_arity(self.db.scriptfns.get(name, []), n)
        if e.kind == "mcall":
            bt = self.static_type_node(f, e.obj)
            cls = None
            if bt is not None:
                cls = bt.inner if bt.kind in ("obj", "ptr", "cls") else (
                    bt.txt if bt.kind in ("struct", "opaque", "String", "Vector") else None)
                if bt.kind in ("int", "float", "bool", "enum"):
                    # never guess a class from the method name alone
                    err("method %s() called on a %s receiver" % (e.name, bt.txt), e)
            n = len(e.args)
            if cls:
                p = self._best_arity(self.db.by_name.get((cls, e.name), []), n)
                if p:
                    return p
            # The receiver's class doesn't declare the method, so prefer one the module
            # declared itself, then the first exact-arity match.  Feeds KNOWN_CALL_DIFFS.
            best = None
            for (c, nm), ps in self.db.by_name.items():
                if nm == e.name and c is not None:
                    for p in ps:
                        if len(p.params) == n:
                            score = (p.text in self.db.declared,
                                     bool(cls) and (c.startswith(cls) or cls.startswith(c)))
                            if best is None or score > best[0]:
                                best = (score, p)
            if best:
                return best[1]
            # Nothing at exact arity, and no hierarchy to search: only now accept a
            # default-fillable match.
            for (c, nm), ps in self.db.by_name.items():
                if nm == e.name and c is not None:
                    for p in ps:
                        if params_match(p, n):
                            return p
            return None

    def find_native(self, cls, name, nargs, required=True):
        for p in self.db.by_name.get((cls, name), []):
            if len(p.params) == nargs:
                return p
        if required:
            err("no native %s::%s with %d arguments in the symbol DB" % (cls, name, nargs))
        return None

    def gen_call(self, f, e, want=None, dest=None):
        if e.fn.kind == "id":
            builtin = self.builtin(f, e, dest)
            if builtin is not None:
                return builtin
        p = self.resolve_call(f, e)
        if p is None:
            err("cannot resolve call to %r/%d" % (getattr(e.fn, "name", "?"), len(e.args)), e)
        is_native = p in self.db.natives
        return self.emit_call(f, p, None, e.args, is_native, e)

    def gen_mcall(self, f, e, want=None):
        p = self.resolve_call(f, e)
        if p is None:
            err("cannot resolve method %r/%d" % (e.name, len(e.args)), e)
        return self.emit_call(f, p, e.obj, e.args, True, e)

    def emit_call(self, f, p, objnode, args, is_native, e):
        if len(args) < len(p.params) and params_match(p, len(args)):
            args = list(args) + [p.defaults[i] for i in range(len(args), len(p.params))]
        if len(args) != len(p.params):
            err("%s takes %d arguments, %d given" % (p.text, len(p.params), len(args)), e)
        # 1. lay out the argument block
        parts = []                   # (kind, Type)
        if p.ret.kind != "void":
            parts.append(("ret", p.ret))
        if is_native and p.this is not None:
            parts.append(("this", p.this))
        for t in p.params:
            parts.append(("arg", t))
        base = f.bptr
        offs = [f.alloc_block(t.size) for _, t in parts]
        end = f.bptr
        f.bptr = end + 8             # the callee's frame header; nested calls go above
        # 2. fill it
        idx = 0
        retoff = None
        cleanup = []                 # String slots to STRFREE after the call
        if p.ret.kind != "void":
            retoff = offs[idx]
            idx += 1
            if p.ret.kind == "String":
                f.emit("STRNEW", sl(retoff, p.ret), imm_int(0))
        if is_native and p.this is not None:
            thisoff = offs[idx]
            idx += 1
            self.gen_this(f, objnode, p, sl(thisoff, p.this))
        for i, t in enumerate(p.params):
            dst = sl(offs[idx + i], t)
            a = args[i]
            direct = dst if self.is_direct(a, t) or a.kind == "addr" else None
            if t.kind == "String":
                f.emit("STRNEW", dst, imm_int(0))
                cleanup.append(dst)
                dst.defer = cleanup
                v = self.gen_expr(f, a, want=T_STRING, dest=direct)
                if v is not dst:
                    f.emit("STRCPY", dst, v)
                    self.release(f, v, dst)
                    self.consume(f, v)
            elif t.kind == "Vector":
                v = self.gen_expr(f, a, want=T_VEC, dest=direct)
                if v is not dst:
                    f.emit("MOVV", dst, v)
                    self.consume(f, v)
            else:
                v = self.gen_expr(f, a, want=t, dest=direct)
                if v is dst:
                    continue
                if t.kind in ("ptr", "obj") and v.type.kind in ("struct", "cls"):
                    f.emit("LEA", dst, v)
                elif t.kind == "float" and v.type.kind == "int":
                    f.emit("ITOF", dst, v)
                else:
                    f.emit("MOV", dst, v)
                self.consume(f, v)
        # 3. the CALL itself
        kind = "N" if is_native else "C"
        tgt = Operand(4, (0, 0), fix=[(kind, p.text)])
        argop = Operand(2, (base & 0xFFFF,)) if end > base else Operand(2, (0xFFFF,))
        f.emit("CALL", tgt, Operand(2, (end & 0xFFFF,)), argop)
        for d in reversed(cleanup):
            f.emit("STRFREE", d)
        if retoff is None:
            f.bptr = base
            return Place(T_VOID, 0, ())
        # the block is released except the result, which stays until consumed
        # (`a.getPos() - b.getPos()`: the second block sits above the first result)
        f.bptr = base + p.ret.size
        r = sl(retoff, p.ret)
        r.bmark = base
        if p.ret.kind == "String":
            r.owned = True
        return r

    def gen_this(self, f, objnode, p, dst):
        if objnode.kind == "cast" and objnode.type.kind in ("obj", "cls"):
            self.gen_expr(f, objnode, want=p.this, dest=dst)     # DYNCAST into the slot
            return
        v = self.gen_expr(f, objnode, want=None)
        # a value object / struct is passed by address; a reference (`@CFoo`) by value
        if v.type.kind in ("struct", "cls", "opaque", "Vector", "String"):
            f.emit("LEA", dst, v)          # value types are passed by address
        else:
            f.emit("MOV", dst, v)
        self.consume(f, v)

    def gen_thread(self, f, call):
        if call.kind != "call" or call.fn.kind != "id":
            err("`thread` needs a direct call to a script function", call)
        name = call.fn.name
        p = self._best_arity(self.db.scriptfns.get(name, []), len(call.args))
        if p is None:
            err("`thread` target %r is not a known script function" % name, call)
        args = call.args
        if len(args) < len(p.params):
            args = list(args) + [p.defaults[i] for i in range(len(args), len(p.params))]
        call = Node("call", fn=call.fn, args=args, line=call.line)
        total = sum(t.size for t in p.params)
        h = f.alloc_temp(8)          # {id, stack}
        f.emit("THREAD", sl(h, T_INT), Operand(4, (0, 0), fix=[("C", p.text)]),
               Operand(2, (total & 0xFFFF,)) if total else Operand(0))
        stack = h + 4
        off = 0
        for i, t in enumerate(p.params):
            dst = Place(t, 7, (stack & 0xFFFF, off & 0xFFFF))
            if t.kind == "Vector":
                f.emit("MOVV", dst, self.gen_expr(f, call.args[i], want=T_VEC))
            elif t.kind == "String":
                f.emit("STRNEW", dst, sl(h, T_INT))      # created on the new thread's stack
                f.emit("STRCPY", dst, self.gen_expr(f, call.args[i], want=T_STRING))
            elif t.kind in ("ptr", "obj"):
                v = self.gen_expr(f, call.args[i], want=None)
                f.emit("LEA" if v.type.kind in ("struct", "cls") else "MOV", dst, v)
            else:
                v = self.gen_expr(f, call.args[i], want=t)
                f.emit("MOV", dst, self.coerce(f, v, t, call.args[i]))
            off += t.size
        return sl(h, T_INT)          # the thread id (`int h = thread f(...);`)

    # ---------------- built-in conversions
    def builtin(self, f, e, dest=None):
        name = e.fn.name
        if name == "dot" and len(e.args) == 2:
            d = dest if dest is not None else sl(f.alloc_temp(4), T_FLOAT)
            a = self.gen_expr(f, e.args[0], want=T_VEC)
            b = self.gen_expr(f, e.args[1], want=T_VEC)
            f.emit("DOT", d, a, b)
            return d
        table = {"toInt": ("FTOI", T_INT), "toFloat": ("ITOF", T_FLOAT),
                 "toString": (None, T_STRING)}
        if name not in table or len(e.args) != 1:
            return None
        mn, rt = table[name]
        mark = f.temp
        d = dest if dest is not None else sl(f.alloc_temp(rt.size), rt)
        if name == "toString" and dest is None:
            f.emit("STRNEW", d, imm_int(0))
            d.owned = True
        v = self.gen_expr(f, e.args[0], want=None)
        if name == "toString":
            mn = {"int": "ITOS", "float": "FTOS", "bool": "BTOS", "Vector": "VTOS"}.get(v.type.kind)
            if mn is None:
                err("toString() does not accept %s" % v.type.txt, e)
        f.emit(mn, d, v)
        self.consume(f, v)
        f.temp = mark + (rt.size if dest is None else 0)
        return d


def _only_goto(s):
    """`if (c) goto L;` (with or without braces) and no `else` -> that `goto`."""
    if s.els is not None:
        return None
    t = s.then
    while t is not None and t.kind == "block" and len(t.body) == 1:
        t = t.body[0]
    return t if (t is not None and t.kind == "goto") else None


def contains_label(s):
    """Does `s` (or anything nested in it) define a label?"""
    if s is None:
        return False
    if s.kind == "label":
        return True
    for k in ("body", "then", "els", "handler", "init", "step"):
        v = getattr(s, k, None)
        if isinstance(v, list):
            if any(contains_label(x) for x in v):
                return True
        elif isinstance(v, Node) and contains_label(v):
            return True
    return False


def falls_through(s):
    """False when control cannot reach the statement after `s`."""
    if s is None:
        return True
    if s.kind in ("return", "break", "continue", "goto"):
        return False
    if s.kind == "label":
        return True
    if s.kind != "block" and contains_label(s):
        return True       # something may jump into it and then fall out
    if s.kind == "block":
        # a label makes what follows reachable again, whatever preceded it
        reach = True
        for x in s.body:
            if x.kind == "label":
                reach = True
            elif reach:
                reach = falls_through(x)
        return reach
    if s.kind == "if":
        if s.els is None:
            return True
        return falls_through(s.then) or falls_through(s.els)
    if s.kind == "try":
        return falls_through(s.body) or falls_through(s.handler)
    if s.kind == "for" and s.cond is None:
        return has_break(s.body)
    if s.kind == "while" and s.cond.kind == "bool" and s.cond.v:
        return has_break(s.body)
    return True


def has_break(s):
    """Does `s` contain a `break` that leaves the loop it is the body of?"""
    if s is None:
        return False
    if s.kind == "break":
        return True
    if s.kind == "block":
        return any(has_break(x) for x in s.body)
    if s.kind == "if":
        return has_break(s.then) or has_break(s.els)
    return False


def proto_text(ret, name, params):
    return "%s %s(%s)" % (ret.txt, name, ",".join(p.txt for p in params))


# ============================================================ module assembly
def build_module(comp, mods, version=1):
    """Turn the generated FuncGens into a dante.Dante module."""
    d = Dante(None, comp.module)
    d.version = version
    for text, (kind, val) in sorted(comp.used_assume.items(), key=lambda kv: kv[0].lower()):
        d.assumptions.append((kind, text, val))
    for i, s in enumerate(comp.strorder):
        d.strings.append((4 * i, s))
    dataoff = 4 * len(comp.strorder)
    for name, (t, sym, own) in comp.globals.items():
        if own:
            d.exports.append(("D", dataoff, sym))
            dataoff += t.size
    d.data_size = dataoff if dataoff else 4
    # code: concatenate, resolving labels to Instr objects
    for proto, f in mods:
        n = len(f.code)
        enter = Instr(NAMEOP["ENTER"], [Operand(2, (max(FuncGen.MINFRAME, f.high + 12) & 0xFFFF,))])
        # resolve label indices -> Instr (None = end of this function)
        for ins in f.code:
            lab = ins.ops[0].label
            if isinstance(lab, Label):
                t = lab.target
                if t is None:
                    raise CompileError("unbound label %s in %s" % (lab, f.name))
                ins.ops[0].label = f.code[t] if t < n else None
        d.exports.append(("C", enter, proto.text))
        f.code.insert(0, enter)
        d.code.extend(f.code)
    # a label bound past the end of a function points at that function's RET,
    # which is always the last instruction we appended for it
    for proto, f in mods:
        for ins in f.code:
            if _is_jump(ins) and ins.ops[0].label is None:
                ins.ops[0].label = f.code[-1]
    _peephole(d)
    _finish(d)
    return d


def _is_jump(ins):
    return DV.OPS[ins.op][1] == "j"


def _is_goto(ins):
    return (ins.op == NAMEOP["JEQ_I"] and ins.ops[1].mode == 0 and ins.ops[2].mode == 0
            and isinstance(ins.ops[0].label, Instr))


def _peephole(d):
    """Drop `goto next` (JEQ_I -> next, #0, #0) and thread jumps whose target is
    itself an unconditional goto, iterating to a fixed point."""
    changed = True
    while changed:
        changed = False
        for ins in d.code:
            if _is_jump(ins) and isinstance(ins.ops[0].label, Instr):
                tgt = ins.ops[0].label
                hops = 0
                while _is_goto(tgt) and tgt.ops[0].label is not tgt and hops < 16:
                    tgt = tgt.ops[0].label
                    hops += 1
                if tgt is not ins.ops[0].label:
                    ins.ops[0].label = tgt
                    changed = True
        out = []
        for i, ins in enumerate(d.code):
            if (ins.op == NAMEOP["JEQ_I"] and ins.ops[1].mode == 0 and ins.ops[2].mode == 0
                    and isinstance(ins.ops[0].label, Instr)):
                nxt = d.code[i + 1] if i + 1 < len(d.code) else None
                if ins.ops[0].label is nxt:
                    # retarget anything pointing at this instruction
                    for other in d.code:
                        if _is_jump(other) and other.ops[0].label is ins:
                            other.ops[0].label = nxt
                    for j, (k, r, t) in enumerate(d.exports):
                        if k == "C" and r is ins:
                            d.exports[j] = (k, nxt, t)
                    changed = True
                    continue
            out.append(ins)
        d.code = out


def _finish(d):
    off = 0
    for ins in d.code:
        ins.off = off
        off += ins.size
    d.code_size = off
    for ins in d.code:
        o = ins.ops[0]
        if isinstance(o.label, Instr):
            o.words = ((o.label.off - ins.off) & 0xFFFF,)
            o.label = None
    d.exports = [(k, (r.off if isinstance(r, Instr) else r), t) for k, r, t in d.exports]
    d.sort_sections()
    d._index()


# ============================================================ driver
def default_symbol_paths():
    """[(kind, path)] loaded into every SymbolDB -- see dante.symbols."""
    return symbols.default_symbol_sources()


def compile_source(path, symbols=(), libs=(), module=None, emit_init=True):
    types = Types()
    db = SymbolDB(types)
    for kind, p in default_symbol_paths():
        (db.load_api if kind == "api" else db.load_json)(p)
    for p in symbols:
        db.load_json(p)
    for p in libs:
        for g in (glob.glob(p) if any(c in p for c in "*?") else [p]):
            db.load_lib(g)
    src = open(path, encoding="latin-1").read()
    toks = lex(src)
    decls = Parser(toks, types).parse_module()
    stem = module or os.path.splitext(os.path.basename(path))[0]
    comp = Compiler(db, types, stem)
    mods = comp.compile(decls, emit_init=emit_init)
    return build_module(comp, mods)


def cmd_compile(args):
    try:
        d = compile_source(args.source, args.symbols, args.lib, args.module,
                           emit_init=not args.no_init)
    except CompileError as ex:
        print("%s: %s" % (args.source, ex), file=sys.stderr)
        return 2
    if args.asm:
        with open(args.out, "w", newline="\n") as fh:
            fh.write(DV.to_asm(d))
    else:
        with open(args.out, "wb") as fh:
            fh.write(d.emit().encode("latin-1"))
    errs = DV.verify_one(d, quiet=True)
    print("%s: %d instrs, %d words, data %d, %d fixup sites%s" % (
        args.out, len(d.code), d.code_size, d.data_size,
        sum(len(s) for _, _, s in d.fixups), "" if not errs else "  (%d VERIFY ERRORS)" % len(errs)))
    for e in errs[:10]:
        print("   " + e)
    return 1 if errs else 0


def register(sub):
    """Mount `dante compile` on the CLI."""
    a = sub.add_parser("compile", help="compile a .dn source module")
    a.add_argument("source")
    a.add_argument("-o", "--out", required=True)
    a.add_argument("-S", "--asm", action="store_true", help="emit assembly (.s) instead of .dante")
    a.add_argument("--symbols", action="append", default=[],
                   help="extra symbol JSON (natives / enums / assumptions)")
    a.add_argument("--lib", action="append", default=[],
                   help="a .dante whose C exports become callable (e.g. world/global.dante)")
    a.add_argument("--module", help="module stem (default: the source file's name)")
    a.add_argument("--no-init", action="store_true",
                   help="do not emit __<stem>_init() (for patches linked into an existing module)")
    a.set_defaults(fn=cmd_compile)
