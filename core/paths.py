"""One instance directory, independent of the shell's working directory."""
from pathlib import Path

from core import env

CODE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path(env.get('HOME') or CODE_ROOT).expanduser().absolute().resolve()


def root():
    from core.config import ROOT
    return Path(ROOT)


def data_path(*parts):
    return root().joinpath('data', *parts)
