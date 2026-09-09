"""The `dante` command.  Each module registers its own subcommands."""
import sys
import argparse

from . import __version__
from . import module, compiler, decompiler
from .compiler import CompileError
from .vm import machine as vm_machine
from .vm.machine import VMFault


def build_parser():
    ap = argparse.ArgumentParser(
        prog="dante",
        description="Dante script toolchain for Ghostbusters: The Video Game Remastered.",
        epilog="docs/dante_vm.md (machine), docs/dante_format.md (container), "
               "docs/dante_lang.md (language), docs/dante_cookbook.md (recipes).")
    ap.add_argument("--version", action="version", version="dante %s" % __version__)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    module.register(sub)
    compiler.register(sub)
    decompiler.register(sub)
    vm_machine.register(sub)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except CompileError as ex:
        print("compile error: %s" % ex, file=sys.stderr)
        return 2
    except VMFault as ex:
        print("VM fault: %s" % ex, file=sys.stderr)
        return 1
    except BrokenPipeError:                       # `dante disasm ... | head`
        return 0


if __name__ == "__main__":
    sys.exit(main())
