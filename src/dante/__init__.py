"""The Dante script toolchain for Ghostbusters: The Video Game Remastered."""
from . import symbols
from . import module
from .module import (
    Dante, Instr, Operand,
    load, iter_files, to_asm, parse_asm,
    link, verify_one, compare_modules, compare_calls, call_targets,
    func_body, normalize, classid, class_table,
)
from .compiler import CompileError, compile_source
from .decompiler import decompile_file, compilable_source

__version__ = "1.0.0"

__all__ = [
    "Dante", "Instr", "Operand",
    "load", "iter_files", "to_asm", "parse_asm",
    "link", "verify_one", "compare_modules", "compare_calls", "call_targets",
    "func_body", "normalize", "classid", "class_table",
    "CompileError", "compile_source",
    "decompile_file", "compilable_source",
    "module", "symbols", "__version__",
]
