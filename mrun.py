#!/usr/bin/env python3
"""mrun — a universal, dynamic meta-runner for CLI tools.
Reads declarative JSON configurations (modes, bases, overlays), applies
named overlays / anonymous patches and CLI overrides, evaluates
placeholders and sentinels, and runs the assembled command with optional
pre/post-execution hooks.
Python 3, standard library only.  See mrun(1).
"""
import argparse
import hashlib
import json
import math          # noqa: F401  (exposed to @mrun_py)
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

PROGRAM = 'mrun.py'
TRUST_TTL_SECONDS = 30 * 24 * 3600

# Sentinel prefixes / markers -------------------------------------------------
S_TRUE = '@mrun_true'
S_FALSE = '@mrun_false'
S_STR = '@mrun_str:'
S_ENV = '@mrun_env '
S_PY = '@mrun_py '
S_LIST = '@mrun_list '
S_REPEAT = '@mrun_repeat '

# --------------------------------------------------------------------------- messages
_quiet = False

def info(msg):
    if not _quiet:
        print(f"[INFO] {msg}", file=sys.stderr)

def warning(msg):
    if not _quiet:
        print(f"[WARNING] {msg}", file=sys.stderr)

def error(msg):
    print(f"[ERROR] {msg}", file=sys.stderr)

class MrunError(Exception):
    """Fatal error; printed as [ERROR] and exits 1."""

# --------------------------------------------------------------------------- coercion / placeholders
_INT_RE = re.compile(r'-?\d+\Z')
_FLOAT_RE = re.compile(r'-?(\d+\.\d*|\.\d+|\d+)([eE][+\-]?\d+)?\Z')
_PLACEHOLDER_RE = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}')
_ENV_NAME_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')

def coerce(value):
    """Coerce unambiguous numeric strings; no boolean coercion."""
    if not isinstance(value, str):
        return value
    if _INT_RE.fullmatch(value):
        return int(value)
    if _FLOAT_RE.fullmatch(value) and ('.' in value or 'e' in value or 'E' in value):
        return float(value)
    return value

def substitute(text, variables, depth=0):
    """Replace {name} placeholders from variables (recursive to depth 10)."""
    def repl(match):
        name = match.group(1)
        if name not in variables:
            return match.group(0)
        v = variables[name]
        if isinstance(v, bool):
            return 'true' if v else 'false'
        if v is None:
            return ''
        if isinstance(v, str):
            if depth + 1 > 10:
                return v
            return substitute(v, variables, depth + 1)
        return str(v)
    return _PLACEHOLDER_RE.sub(repl, text)

# --------------------------------------------------------------------------- sentinel evaluation
def eval_py(expr, ctx):
    try:
        return eval(expr, PY_GLOBALS, dict(ctx))
    except MrunError:
        raise
    except Exception as exc:
        raise MrunError(f"Error evaluating '@mrun_py {expr}': {exc}")

def eval_env(text, ctx):
    rest = text[len(S_ENV):]
    parts = rest.split(None, 1)
    if not parts:
        raise MrunError("Malformed '@mrun_env' sentinel: missing variable name")
    name = parts[0]
    if not _ENV_NAME_RE.fullmatch(name):
        raise MrunError(f"Malformed '@mrun_env' sentinel: invalid variable name '{name}'")
    if name in os.environ:                      # set (even to empty) -> verbatim
        return os.environ[name]
    if len(parts) > 1:
        default = parts[1].strip()
        if default:                             # placeholders + coercion only
            return coerce(substitute(default, ctx['vars']))
    raise MrunError(
        f"'@mrun_env {name}': environment variable '{name}' is not set "
        f"and no default was provided")

def eval_value(raw, ctx, cli=False):
    """Evaluate one configuration/override value."""
    if isinstance(raw, list):
        return [eval_value(item, ctx, cli) for item in raw]
    if isinstance(raw, dict):
        return raw                              # objects pass through unchanged
    if not isinstance(raw, str):
        return raw                              # numbers / booleans / null
    if raw == S_TRUE:
        return True
    if raw == S_FALSE:
        return False
    if raw.startswith(S_STR):
        return raw[len(S_STR):]                 # verbatim, nothing else
    if raw == '@mrun_env':
        raise MrunError("Malformed '@mrun_env' sentinel: missing variable name")
    if raw.startswith(S_ENV):
        return eval_env(raw, ctx)
    if raw.startswith(S_PY):
        return eval_py(raw[len(S_PY):], ctx)
    if raw.startswith(S_LIST):
        if not cli:
            raise MrunError("'@mrun_list' is only valid in CLI overrides; "
                            "use a JSON array in configuration files")
        try:
            return shlex.split(raw[len(S_LIST):])
        except ValueError as exc:
            raise MrunError(f"'@mrun_list': shell splitting failed: {exc}")
    return coerce(substitute(raw, ctx['vars']))

PY_GLOBALS = {
    'argparse': argparse, 'hashlib': hashlib, 'json': json, 'os': os,
    're': re, 'sys': sys, 'math': math, 'subprocess': subprocess,
    'time': time, 'shlex': shlex, 'Path': Path,
    # mrun helper functions
    'coerce': coerce, 'substitute': substitute, 'quote': shlex.quote,
}

# --------------------------------------------------------------------------- configuration scan / load
class ConfigSpace:
    def __init__(self, config_dir):
        self.dir = config_dir
        self.order = []          # scan order (sorted full paths)
        self.by_path = {}
        self.by_name = {}
        self.cache = {}          # resolved path -> parsed JSON (at most once)

    def load(self, path):
        rp = Path(path).resolve()
        if rp in self.cache:
            return rp, self.cache[rp]
        try:
            text = rp.read_text(encoding='utf-8')
        except OSError as exc:
            raise MrunError(f"Cannot read '{rp}': {exc.strerror or exc}")
        try:
            cfg = json.loads(text)
        except ValueError as exc:
            raise MrunError(f"Malformed JSON in '{rp}': {exc}")
        self.cache[rp] = cfg
        return rp, cfg

    def scan(self):
        if not (self.dir and self.dir.is_dir()):
            return
        try:
            candidates = sorted(self.dir.rglob('*.json'))
        except OSError as exc:
            raise MrunError(
                f"Cannot scan configuration directory '{self.dir}': {exc.strerror or exc}")
        for path in candidates:
            if not path.is_file():
                continue
            rp, cfg = self.load(path)
            if rp in self.by_path:
                continue
            if not isinstance(cfg, dict):
                raise MrunError(f"'{rp}': configuration must be a JSON object")
            kind = cfg.get('kind')
            if kind not in ('mode', 'base', 'overlay'):
                raise MrunError(f"'{rp}': missing or invalid 'kind' "
                                f"(must be exactly one of 'mode', 'base', 'overlay')")
            name = cfg.get('name')
            if not isinstance(name, str) or not name:
                raise MrunError(f"'{rp}': missing or empty 'name'")
            if name in self.by_name:
                other = self.by_name[name][0]
                raise MrunError(f"Duplicate configuration name '{name}' ('{other}' and '{rp}')")
            self.order.append(rp)
            self.by_path[rp] = cfg
            self.by_name[name] = (rp, cfg)

# --------------------------------------------------------------------------- include / chain resolution
def resolve_include_path(entry, base_dir, from_file):
    p = base_dir / entry
    if not p.exists() and p.suffix != '.json':
        alt = p.with_suffix('.json')
        if alt.exists():
            p = alt
    if not p.is_file():
        raise MrunError(f"Include not found: '{entry}' (from {from_file})")
    return p.resolve()

def build_chain(space, root, seen, on_load=None, _stack=None):
    """Depth-first, post-order include chain with a shared *seen* set."""
    stack = _stack if _stack is not None else []
    rp = Path(root).resolve()
    if rp in seen:
        return []
    if rp in stack:
        cycle = stack[stack.index(rp):] + [rp]
        raise MrunError('Circular include detected: ' + ' -> '.join(str(p) for p in cycle))
    rp, cfg = space.load(rp)
    chain = []
    if isinstance(cfg, dict):
        inc = cfg.get('include')
        if inc is None:
            entries = []
        elif isinstance(inc, str):
            entries = [inc]
        elif isinstance(inc, list):
            entries = inc
        else:
            raise MrunError(f"'{rp}': 'include' must be a string or a list of strings")
    else:
        entries = []
    stack.append(rp)
    try:
        for entry in entries:
            ip = resolve_include_path(entry, rp.parent, rp)
            chain.extend(build_chain(space, ip, seen, on_load, stack))
    finally:
        stack.pop()
    seen.add(rp)
    chain.append(rp)
    if on_load:
        on_load(rp)
    return chain

# --------------------------------------------------------------------------- mode / overlay / patch resolution
def _is_explicit(s):
    return s.startswith('/') or s.startswith('./') or s.startswith('../') or s.startswith('~')

def resolve_mode(space, mode):
    # 1. name match
    if mode in space.by_name:
        path, cfg = space.by_name[mode]
        kind = cfg['kind']
        if kind == 'mode':
            return path
        if kind == 'overlay':
            raise MrunError(f"Configuration '{mode}' is an overlay. "
                            f"Use --overlay {mode} instead.")
        raise MrunError(f"Configuration '{mode}' is a base (include-only); "
                        f"it cannot be selected with -m.")
    # 2. explicit path
    if _is_explicit(mode):
        p = Path(os.path.expanduser(mode)).resolve()
        if p.suffix != '.json' or not p.is_file():
            raise MrunError(f"Configuration '{mode}' not found")
        rp, cfg = space.load(p)
        if not isinstance(cfg, dict) or cfg.get('kind') != 'mode':
            raise MrunError(f"Configuration '{mode}' is not a mode ('kind' must be 'mode')")
        return rp
    # 3. namespace path or stem
    candidate = space.dir / mode
    if candidate.suffix != '.json':
        candidate = candidate.with_suffix('.json')
    candidate = candidate.resolve()
    if candidate.is_file():
        rp, cfg = space.load(candidate)
        if isinstance(cfg, dict) and cfg.get('kind') == 'mode':
            return rp
        raise MrunError(f"Configuration '{mode}' not found")   # kind mismatch: no further search
    for path in space.order:
        try:
            rel = path.relative_to(space.dir).as_posix()
        except ValueError:
            rel = None
        if path.stem == mode or rel == mode or rel == mode + '.json':
            if space.by_path[path]['kind'] == 'mode':
                return path
    raise MrunError(f"Configuration '{mode}' not found")

def resolve_overlay(space, name):
    if name not in space.by_name:
        raise MrunError(f"Overlay '{name}' not found (see --list-overlays)")
    path, cfg = space.by_name[name]
    kind = cfg['kind']
    if kind == 'mode':
        raise MrunError(f"'{name}' is a mode. Use -m {name} instead of --overlay.")
    if kind == 'base':
        raise MrunError(f"'{name}' is a base (include-only); "
                        f"it cannot be applied with --overlay.")
    return path, cfg

def resolve_patch(space, arg):
    if _is_explicit(arg):
        p = Path(os.path.expanduser(arg)).resolve()
        if p.is_file():
            return p
        raise MrunError(f"Patch file not found: '{arg}'")
    candidate = space.dir / arg
    if candidate.suffix != '.json':
        candidate = candidate.with_suffix('.json')
    candidate = candidate.resolve()
    if candidate.is_file():
        return candidate
    for path in space.order:
        try:
            rel = path.relative_to(space.dir).as_posix()
        except ValueError:
            rel = None
        if path.stem == arg or rel == arg or rel == arg + '.json':
            return path
    raise MrunError(f"Patch file not found: '{arg}'")

def check_overlay_compat(space, cfg, mode_chain, strict):
    of = cfg.get('overlay_for') if isinstance(cfg, dict) else None
    if of is None:
        return                                    # compatible with all modes
    entries = [of] if isinstance(of, str) else (of if isinstance(of, list) else [])
    for path in mode_chain:
        candidates = {str(path), path.name}
        try:
            candidates.add(path.relative_to(space.dir).as_posix())
        except ValueError:
            pass
        ccfg = space.cache.get(path)
        if isinstance(ccfg, dict):
            nm = ccfg.get('name')
            if isinstance(nm, str) and nm:
                candidates.add(nm)
        for entry in entries:
            if isinstance(entry, str) and entry in candidates:
                return
    name = cfg.get('name') if isinstance(cfg, dict) else None
    msg = f"Overlay '{name}' is not compatible with this mode (overlay_for: {entries!r})"
    if strict:
        raise MrunError(msg)
    warning(msg)

# --------------------------------------------------------------------------- CLI override parsing
class PreParsed:
    __slots__ = ('value', 'raw')
    def __init__(self, value, raw):
        self.value = value
        self.raw = raw

class VarOverride:
    __slots__ = ('key', 'value', 'raw')
    def __init__(self, key, value, raw):
        self.key = key
        self.value = value
        self.raw = raw

class FlagOverride:
    __slots__ = ('flag', 'bare', 'value', 'raw')
    def __init__(self, flag, bare, value=None, raw=None):
        self.flag = flag
        self.bare = bare
        self.value = value
        self.raw = raw

def _looks_like_flag(tok):
    return isinstance(tok, str) and tok.startswith('--') and len(tok) > 2

def parse_overrides(tokens):
    """Tokenize everything after '--'.  @mrun_list is parsed immediately."""
    var_overrides, flag_overrides, positionals = [], [], []
    n = len(tokens)

    def preparsed(tok):
        if isinstance(tok, str) and tok.startswith(S_LIST):
            try:
                items = shlex.split(tok[len(S_LIST):])
            except ValueError as exc:
                raise MrunError(f"'@mrun_list': shell splitting failed: {exc}")
            return PreParsed(items, tok)
        return tok

    i = 0
    while i < n:
        tok = tokens[i]
        if _looks_like_flag(tok):
            if tok == '--var':
                if i + 1 >= n:
                    raise MrunError("Invalid override '--var': a key=value token is required")
                spec = tokens[i + 1]
                j = i + 2
                if '=' not in spec:
                    raise MrunError(f"Invalid override '--var {spec}': expected key=value")
                key, _, val = spec.partition('=')
                if not key:
                    raise MrunError(f"Invalid override '--var {spec}': empty key")
                if '@mrun_repeat' in key:
                    raise MrunError("The '@mrun_repeat' marker is not allowed in CLI overrides")
                if val == '@mrun_py' and j < n and not _looks_like_flag(tokens[j]):
                    value = raw = S_PY + tokens[j]
                    i = j + 1
                else:
                    pp = preparsed(val)
                    value = pp
                    raw = pp.raw if isinstance(pp, PreParsed) else val
                    i = j
                var_overrides.append(VarOverride(key, value, raw))
                continue
            # ordinary flag
            j = i + 1
            if j >= n or _looks_like_flag(tokens[j]):
                flag_overrides.append(FlagOverride(tok, True))
                i = j
            else:
                val_tok = tokens[j]
                k = j + 1
                if val_tok == '@mrun_py' and k < n and not _looks_like_flag(tokens[k]):
                    value = raw = S_PY + tokens[k]
                    i = k + 1
                else:
                    pp = preparsed(val_tok)
                    value = pp
                    raw = pp.raw if isinstance(pp, PreParsed) else val_tok
                    i = k
                flag_overrides.append(FlagOverride(tok, False, value, raw))
        else:
            positionals.append(preparsed(tok))
            i += 1
    return var_overrides, flag_overrides, positionals

# --------------------------------------------------------------------------- evaluation model
class State:
    def __init__(self):
        self.variables = {}
        self.env = {}
        self.args = {}               # flag -> evaluated value (always-on, ordered)
        self.optional = {}           # flag -> {'value':…, 'raw':…}
        self.repeat = set()          # sticky repeat marking
        self.executable = None
        self.has_executable = False
        self.pre_run = None
        self.post_run = None
        self.positionals = []
        self.run_name = None
        # raw views (for -s / -S hook templates)
        self.raw_vars = {}
        self.raw_env = {}
        self.raw_args = {}           # flag -> {'raw':…, 'repeat':bool}
        self.raw_optional = {}
        self.raw_executable = None
        self.raw_pre_run = None
        self.raw_post_run = None
        self.raw_positionals = []

def evaluate(space, layers, var_overrides, flag_overrides, positionals):
    state = State()

    def ctx(val=None):
        return {'val': val, 'vars': state.variables, 'args': state.args}

    def cli_value(value, val):
        if isinstance(value, PreParsed):
            return value.value
        return eval_value(value, ctx(val), cli=True)

    def parse_key(key, path):
        if key == '@mrun_repeat':
            raise MrunError(f"'{path}': malformed '@mrun_repeat' key (missing flag)")
        if key.startswith('@mrun_repeat'):
            if not key.startswith(S_REPEAT):
                raise MrunError(f"'{path}': malformed '@mrun_repeat' key '{key}'")
            flag = key[len(S_REPEAT):]
            if not flag or flag != flag.strip():
                raise MrunError(f"'{path}': malformed '@mrun_repeat' key '{key}' "
                                f"(flag must not have leading/trailing whitespace)")
            return flag, True
        return key, False

    # run name: last layer declaring one wins
    for path in layers:
        cfg = space.cache.get(path)
        if isinstance(cfg, dict):
            nm = cfg.get('name')
            if isinstance(nm, str) and nm:
                state.run_name = nm

    # ---- section 1: vars (layers, then --var, then variable-matching --key)
    for path in layers:
        cfg = space.cache.get(path)
        if not isinstance(cfg, dict):
            continue
        vs = cfg.get('vars')
        if vs is None:
            continue
        if not isinstance(vs, dict):
            raise MrunError(f"'{path}': 'vars' must be an object")
        for key, raw in vs.items():
            if '@mrun_repeat' in key:
                raise MrunError(f"'{path}': the '@mrun_repeat' marker is not allowed on 'vars' keys")
            state.variables[key] = eval_value(raw, ctx(state.variables.get(key)))
            state.raw_vars[key] = raw
    for vo in var_overrides:
        state.variables[vo.key] = cli_value(vo.value, state.variables.get(vo.key))
        state.raw_vars[vo.key] = vo.raw

    remaining_flags = []
    for fo in flag_overrides:
        varname = fo.flag[2:]
        if varname in state.variables:
            if fo.bare:
                state.variables[varname] = True
                state.raw_vars[varname] = S_TRUE
            else:
                state.variables[varname] = cli_value(fo.value, state.variables[varname])
                state.raw_vars[varname] = fo.raw
        else:
            remaining_flags.append(fo)

    # ---- section 2: env
    for path in layers:
        cfg = space.cache.get(path)
        if not isinstance(cfg, dict):
            continue
        env = cfg.get('env')
        if env is None:
            continue
        if not isinstance(env, dict):
            raise MrunError(f"'{path}': 'env' must be an object")
        for key, raw in env.items():
            if '@mrun_repeat' in key:
                raise MrunError(f"'{path}': the '@mrun_repeat' marker is not allowed on 'env' keys")
            state.env[key] = eval_value(raw, ctx(state.env.get(key)))
            state.raw_env[key] = raw

    # ---- section 3: args + optional_args, then CLI arg overrides
    def set_arg(flag, value, raw):
        if flag in state.optional and flag not in state.args:
            del state.optional[flag]
            state.raw_optional.pop(flag, None)
        state.args[flag] = value
        state.raw_args[flag] = {'raw': raw, 'repeat': flag in state.repeat}

    for path in layers:
        cfg = space.cache.get(path)
        if not isinstance(cfg, dict):
            continue
        a = cfg.get('args')
        o = cfg.get('optional_args')
        if a is not None and not isinstance(a, dict):
            raise MrunError(f"'{path}': 'args' must be an object")
        if o is not None and not isinstance(o, dict):
            raise MrunError(f"'{path}': 'optional_args' must be an object")
        aflags, oflags = {}, {}
        for key in (a or {}):
            flag, _ = parse_key(key, path)
            aflags[flag] = key
        for key in (o or {}):
            flag, _ = parse_key(key, path)
            if not (flag.startswith('--') and len(flag) > 2):
                raise MrunError(f"'{path}': invalid 'optional_args' key '{key}' "
                                f"(must be a '--' flag longer than two characters)")
            oflags[flag] = key
        both = sorted(set(aflags) & set(oflags))
        if both:
            raise MrunError(f"'{path}': flag(s) declared in both 'args' and 'optional_args': "
                            + ', '.join(both))
        for key, raw in (a or {}).items():
            flag, rep = parse_key(key, path)
            if rep:
                state.repeat.add(flag)
            if flag in state.optional and flag not in state.args:
                prev = state.optional[flag]['value']        # promote
                del state.optional[flag]
                state.raw_optional.pop(flag, None)
                state.args[flag] = eval_value(raw, ctx(prev))
            else:
                state.args[flag] = eval_value(raw, ctx(state.args.get(flag)))
            state.raw_args[flag] = {'raw': raw, 'repeat': flag in state.repeat}
        for key, raw in (o or {}).items():
            flag, rep = parse_key(key, path)
            if rep:
                state.repeat.add(flag)
            if flag in state.args:                          # demote
                prev = state.args[flag]
                del state.args[flag]
                state.raw_args.pop(flag, None)
                state.optional[flag] = {'value': eval_value(raw, ctx(prev)), 'raw': raw}
            else:
                prev = state.optional[flag]['value'] if flag in state.optional else None
                state.optional[flag] = {'value': eval_value(raw, ctx(prev)), 'raw': raw}
            state.raw_optional[flag] = {'raw': raw, 'repeat': flag in state.repeat}

    first_repeat = set()
    for fo in remaining_flags:
        flag = fo.flag
        dormant = flag in state.optional and flag not in state.args
        if fo.bare:
            if dormant:
                opt = state.optional[flag]
                if opt['value'] is None:
                    raise MrunError(f"Optional flag '{flag}' has no default value; "
                                    f"an explicit value is required")
                set_arg(flag, opt['value'], opt['raw'])
            else:
                set_arg(flag, True, S_TRUE)
        elif flag in state.repeat:
            if flag not in first_repeat:                    # first occurrence replaces
                first_repeat.add(flag)
                val = None if dormant else state.args.get(flag)
                set_arg(flag, cli_value(fo.value, val), fo.raw)
            else:                                           # subsequent occurrences append
                acc = state.args.get(flag)
                newv = cli_value(fo.value, acc)
                base = list(acc) if isinstance(acc, list) else ([] if acc is None else [acc])
                if isinstance(newv, list):
                    base.extend(newv)
                else:
                    base.append(newv)
                state.args[flag] = base
                state.raw_args[flag] = {'raw': fo.raw, 'repeat': True}
        else:
            val = None if dormant else state.args.get(flag)
            set_arg(flag, cli_value(fo.value, val), fo.raw)

    # ---- section 4: executable
    for path in layers:
        cfg = space.cache.get(path)
        if isinstance(cfg, dict) and 'executable' in cfg:
            raw = cfg['executable']
            state.executable = eval_value(raw, ctx(state.executable))
            state.has_executable = True
            state.raw_executable = raw

    # ---- section 5: hooks (collected raw; later layer replaces entirely)
    for path in layers:
        cfg = space.cache.get(path)
        if not isinstance(cfg, dict):
            continue
        if 'pre_run' in cfg:
            v = cfg['pre_run']
            state.pre_run = [v] if isinstance(v, str) else v
            state.raw_pre_run = state.pre_run
        if 'post_run' in cfg:
            v = cfg['post_run']
            state.post_run = [v] if isinstance(v, str) else v
            state.raw_post_run = state.post_run

    # ---- section 6: positional arguments (CLI only)
    for tok in positionals:
        if isinstance(tok, PreParsed):
            state.positionals.append(tok.value)
            state.raw_positionals.append(tok.raw)
        else:
            state.positionals.append(eval_value(tok, ctx(None), cli=True))
            state.raw_positionals.append(tok)

    return state

# --------------------------------------------------------------------------- command construction
def stringify(v):
    return str(v)

def emit_arg(cmd, flag, value, repeat):
    if repeat:
        if isinstance(value, (list, tuple)):
            for el in value:
                if isinstance(el, (list, tuple)):
                    for sub in el:
                        if isinstance(sub, (list, tuple, dict)):
                            raise MrunError(f"Argument '{flag}': nesting deeper than "
                                            f"one level is not allowed")
                    if not el:
                        continue
                    cmd.append(flag)
                    cmd.extend(stringify(x) for x in el)
                elif el is True:
                    cmd.append(flag)
                elif el is False or el is None:
                    continue
                else:
                    cmd.extend([flag, stringify(el)])
        elif value is True:
            cmd.append(flag)
        elif value is False or value is None:
            pass
        elif isinstance(value, (list, tuple)):
            for el in value:
                if isinstance(el, (list, tuple, dict)):
                    raise MrunError(f"Argument '{flag}': nested list/dict values are not "
                                    f"allowed (use the '@mrun_repeat' marker)")
            cmd.append(flag)
            cmd.extend(stringify(x) for x in value)
        else:
            cmd.extend([flag, stringify(value)])
    else:
        if value is True:
            cmd.append(flag)
        elif value is False or value is None:
            pass
        elif isinstance(value, (list, tuple)):
            for el in value:
                if isinstance(el, (list, tuple, dict)):
                    raise MrunError(f"Argument '{flag}': nested list/dict values are not "
                                    f"allowed (use the '@mrun_repeat' marker)")
            cmd.append(flag)
            cmd.extend(stringify(x) for x in value)
        else:
            cmd.extend([flag, stringify(value)])

def env_assignment_prefix(state):
    """Render state.env as 'KEY=value ' shell assignments for copy-paste display.

    Returns an empty string when no env vars are set.  Values are shell-quoted
    so the resulting command can be pasted into a shell and run directly.
    """
    if not state.env:
        return ''
    parts = []
    for key, value in state.env.items():
        parts.append(f"{key}={shlex.quote(str(value))}")
    return ' '.join(parts) + ' '

def build_command(state):
    cmd = [stringify(state.executable)]
    for flag, value in state.args.items():
        emit_arg(cmd, flag, value, flag in state.repeat)
    for p in state.positionals:
        cmd.append(stringify(p))
    command_only = ' '.join(shlex.quote(c) for c in cmd)
    cmd_str = env_assignment_prefix(state) + command_only
    return cmd, cmd_str

# --------------------------------------------------------------------------- dynamic content trust
def trust_store_path():
    return Path.home() / '.config' / 'mrun' / 'trusted_configs.json'

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()

def load_trust_store():
    try:
        with open(trust_store_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}

def save_trust_store(store):
    now = time.time()
    pruned = {}
    for key, rec in store.items():
        if isinstance(rec, dict):
            trusted_at = rec.get('trusted_at')
            if isinstance(trusted_at, (int, float)) and now - trusted_at <= TRUST_TTL_SECONDS:
                pruned[key] = rec
    path = trust_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(pruned, f, indent=2, sort_keys=True)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

def record_valid(rec, path, now):
    if not isinstance(rec, dict):
        return False
    trusted_at = rec.get('trusted_at')
    if not isinstance(trusted_at, (int, float)) or now - trusted_at > TRUST_TTL_SECONDS:
        return False
    try:
        st = path.stat()
        digest = sha256_of(path)
    except OSError:
        return False
    return rec.get('hash') == digest and rec.get('mtime') == st.st_mtime

def contains_py(value):
    if isinstance(value, str):
        return value.startswith(S_PY)
    if isinstance(value, list):
        return any(contains_py(v) for v in value)
    if isinstance(value, dict):
        return any(contains_py(v) for v in value.values())   # keys are not scanned
    return False

def has_dynamic_content(cfg):
    if not isinstance(cfg, dict):
        return False
    for section in ('vars', 'env', 'args', 'optional_args'):
        if section in cfg and contains_py(cfg[section]):
            return True
    exe = cfg.get('executable')
    if isinstance(exe, str) and exe.startswith(S_PY):
        return True
    if 'pre_run' in cfg or 'post_run' in cfg:
        return True
    return False

def trust_gate(space, layers, assume_trusted):
    if assume_trusted:
        return
    dynamic = [p for p in layers if has_dynamic_content(space.cache.get(p))]
    if not dynamic:
        return
    store = load_trust_store()
    now = time.time()
    untrusted = [p for p in dynamic if not record_valid(store.get(str(p)), p, now)]
    if not untrusted:
        return
    sep = '=' * 62
    print(sep, file=sys.stderr)
    print('[SECURITY] Dynamic code detected in configuration.', file=sys.stderr)
    print(sep, file=sys.stderr)
    print('Untrusted file(s) containing dynamic content:', file=sys.stderr)
    for p in untrusted:
        print(f'• {p}', file=sys.stderr)
    print('Options:', file=sys.stderr)
    print('[D] Deny            — abort immediately', file=sys.stderr)
    print('[A] Allow once      — proceed, do not remember', file=sys.stderr)
    print('[T] Trust 30 days   — remember these files', file=sys.stderr)
    sys.stderr.write('Choice (D/a/t): ')
    sys.stderr.flush()
    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        line = ''
    if line == '':
        print('Denied (no interactive input).', file=sys.stderr)
        sys.exit(1)
    answer = line.strip().lower()
    if answer == 'a':
        return
    if answer == 't':
        for p in untrusted:
            st = p.stat()
            store[str(p)] = {'hash': sha256_of(p), 'mtime': st.st_mtime,
                             'trusted_at': int(now)}
        save_trust_store(store)
        info('Trust saved for 30 days.')
        return
    print('Denied.', file=sys.stderr)
    sys.exit(1)

# --------------------------------------------------------------------------- hook execution
def execute_hooks(hooks, state, env, phase, ret=None, ok=None):
    """Returns (success, code)."""
    if hooks is None:
        return True, 0
    items = hooks if isinstance(hooks, list) else [hooks]
    ctx = {'val': None, 'vars': state.variables, 'args': state.args,
           'ret': ret, 'ok': ok}
    for raw in items:
        evaluated = eval_value(raw, ctx)          # expression errors are fatal (exit 1)
        if evaluated is None:
            continue                              # skip
        if isinstance(evaluated, bool):
            code = 1 if evaluated else 0
            if code != 0:
                warning(f"{phase} hook returned {code}: {raw!r}")
                return False, code
            continue
        if isinstance(evaluated, int):
            if evaluated != 0:
                warning(f"{phase} hook returned {evaluated}: {raw!r}")
                return False, evaluated
            continue
        if isinstance(evaluated, str):
            try:
                proc = subprocess.run(evaluated, shell=True, env=env)
                code = proc.returncode
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                warning(f"{phase} hook raised {type(exc).__name__}: {raw!r}")
                return False, 1
            if code != 0:
                warning(f"{phase} hook failed with exit code {code}: {raw!r}")
                return False, code
            continue
        if isinstance(evaluated, list):
            argv = [str(x) for x in evaluated]
            try:
                proc = subprocess.run(argv, env=env)
                code = proc.returncode
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                warning(f"{phase} hook raised {type(exc).__name__}: {raw!r}")
                return False, 1
            if code != 0:
                warning(f"{phase} hook failed with exit code {code}: {raw!r}")
                return False, code
            continue
        warning(f"{phase} hook evaluated to unsupported type "
                f"'{type(evaluated).__name__}': {raw!r}")
        return False, 1
    return True, 0

# --------------------------------------------------------------------------- execution
def format_duration(elapsed):
    total = int(elapsed)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days > 0:
        core = f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
    elif hours > 0:
        core = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    else:
        core = f"{minutes:02d}:{seconds:02d}"
    return f"Execution time: {core} ({elapsed:.1f}s)"

def execute(state, cmd, cmd_str, no_time, assume_yes):
    base_env = dict(os.environ)
    for key, value in state.env.items():
        base_env[key] = str(value)
    dormant = [flag for flag in state.optional if flag not in state.args]
    if dormant:
        info(f"{len(dormant)} optional argument(s) available, not passed: "
             f"{', '.join(dormant)} (see --list-optional-args)")
    if assume_yes:
        info(f"Executing: {cmd_str}")
    else:
        print("=== Command (Resolved) ===", file=sys.stderr)
        print(cmd_str, file=sys.stderr)
        sys.stderr.write("Run this command? [y/N]: ")
        sys.stderr.flush()
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            line = None
        if line is None or line == '':
            if line is None:
                print("Aborted.", file=sys.stderr)
            else:
                error("Confirmation required; use -y/--yes/--no-confirm for unattended runs.")
                sys.exit(1)
        if line.strip().lower() not in ('y', 'yes'):
            print("Aborted.", file=sys.stderr)
            sys.exit(1)
    start = time.monotonic()
    pre_env = {k: v for k, v in base_env.items() if k not in ('MRUN_RET', 'MRUN_OK')}
    ok_pre, code = execute_hooks(state.pre_run, state, pre_env, 'pre_run')
    if not ok_pre:
        return code                               # executable & post_run skipped; no timing
    try:
        proc = subprocess.run(cmd, env=base_env)
        ret = proc.returncode
    except KeyboardInterrupt:
        info("Interrupted by user")
        ret = 130
    except FileNotFoundError:
        error(f"Executable not found: {cmd[0]}")
        ret = 127
    post_env = dict(base_env)
    post_env['MRUN_RET'] = str(ret)
    post_env['MRUN_OK'] = 'true' if ret == 0 else 'false'
    execute_hooks(state.post_run, state, post_env, 'post_run', ret=ret, ok=(ret == 0))
    if not no_time:
        elapsed = time.monotonic() - start
        print("", file=sys.stderr)
        print(format_duration(elapsed), file=sys.stderr)
    return ret

# --------------------------------------------------------------------------- output: listings, -loa, dry runs
def print_listing(space, kind):
    rows = []
    for name, (path, cfg) in space.by_name.items():
        if cfg.get('kind') != kind:
            continue
        try:
            rel = path.relative_to(space.dir).as_posix()
        except ValueError:
            rel = str(path)
        rows.append((name, rel, cfg.get('description')))
    rows.sort(key=lambda r: r[0])
    if not rows:
        return
    wname = max(len(r[0]) for r in rows)
    wpath = max(len(r[1]) for r in rows)
    for name, rel, desc in rows:
        line = name.ljust(wname) + '  ' + rel.ljust(wpath)
        if desc is None:
            line = line.rstrip()
        else:
            line += '  "' + str(desc) + '"'
        print(line)

def print_loa(state, label):
    print(f"Optional arguments for '{label}' (not passed by default; activate after '--'):")
    if not state.optional:
        print("  (none)")
        return
    rows = []
    for flag, entry in state.optional.items():
        value = entry['value']
        if value is True:
            disp = "(flag, no value)"
        elif value is None:
            disp = "(no default; value required)"
        else:
            disp = repr(value)
        status = "(ACTIVE)" if flag in state.args else "(not passed)"
        rows.append((flag, disp, status))
    wflag = max(len(r[0]) for r in rows)
    wdisp = max(len(r[1]) for r in rows)
    for flag, disp, status in rows:
        print(f"{flag.ljust(wflag)}  {disp.ljust(wdisp)}  {status}")
    print("Activate with:")
    print("mrun.py -m <mode> -- <flag>            # bare: uses default")
    print("mrun.py -m <mode> -- <flag> <value>     # explicit override")

def _hook_lines(hooks, quoted):
    if hooks is None:
        return []
    items = hooks if isinstance(hooks, list) else [hooks]
    if quoted:
        return [f"[{i}] {item!r}" for i, item in enumerate(items)]
    return [f"[{i}] {item}" for i, item in enumerate(items)]

def print_dry_run(state, resolved, cmd_str, print_command=True):
    sections = []
    if resolved:
        sections.append(("Variables (Resolved)",
                         [f"{k}={v!r}" for k, v in state.variables.items()]))
        sections.append(("Environment Variables (Resolved)",
                         [f"{k} {v}" for k, v in state.env.items()]))
        alines = []
        for flag, value in state.args.items():
            prefix = '@mrun_repeat ' if flag in state.repeat else ''
            if value is True:
                alines.append(f"{prefix}{flag}")
            elif value is False or value is None:
                continue
            else:
                alines.append(f"{prefix}{flag} {value!r}")
        sections.append(("Arguments (Resolved)", alines))
        olines = []
        for flag, entry in state.optional.items():
            prefix = '@mrun_repeat ' if flag in state.repeat else ''
            status = "[ACTIVE]" if flag in state.args else "[not passed]"
            olines.append(f"{prefix}{flag} {entry['value']!r}  {status}")
        sections.append(("Optional Arguments (Resolved)", olines))
        sections.append(("Executable (Resolved)", [str(state.executable)]))
        sections.append(("Pre-Run Hooks (Resolved)", _hook_lines(state.pre_run, quoted=False)))
        sections.append(("Post-Run Hooks (Resolved)", _hook_lines(state.post_run, quoted=False)))
        sections.append(("Positional Arguments (Resolved)",
                         [f"[{i}] {v!r}" for i, v in enumerate(state.positionals)]))
    else:
        sections.append(("Variables (Raw)",
                         [f"{k}={v!r}" for k, v in state.raw_vars.items()]))
        sections.append(("Environment Variables (Raw)",
                         [f"{k} {v!r}" for k, v in state.raw_env.items()]))
        alines = []
        for flag, entry in state.raw_args.items():
            prefix = '@mrun_repeat ' if entry['repeat'] else ''
            raw = entry['raw']
            if raw is True:
                alines.append(f"{prefix}{flag}")
            else:
                alines.append(f"{prefix}{flag} {raw!r}")
        sections.append(("Arguments (Raw)", alines))
        olines = []
        for flag, entry in state.raw_optional.items():
            prefix = '@mrun_repeat ' if entry['repeat'] else ''
            status = "[ACTIVE]" if flag in state.args else "[not passed]"
            olines.append(f"{prefix}{flag} {entry['raw']!r}  {status}")
        sections.append(("Optional Arguments (Raw)", olines))
        sections.append(("Executable (Raw)",
                         [repr(state.raw_executable)] if state.raw_executable is not None else []))
        sections.append(("Pre-Run Hooks (Raw)", _hook_lines(state.raw_pre_run, quoted=True)))
        sections.append(("Post-Run Hooks (Raw)", _hook_lines(state.raw_post_run, quoted=True)))
        sections.append(("Positional Arguments (Raw)",
                         [f"[{i}] {t!r}" for i, t in enumerate(state.raw_positionals)]))
    for title, lines in sections:
        print(f"{title}:")
        if lines:
            for line in lines:
                print(f"  {line}")
        else:
            print("  (none)")
        print()
    if print_command:
        print("Command (Resolved):")
        print(f"  {cmd_str}")

# --------------------------------------------------------------------------- argument parsing
class LayerAction(argparse.Action):
    """Collect -o/--overlay and -p/--patch in command-line order."""
    def __call__(self, parser, namespace, values, option_string=None):
        layers = getattr(namespace, self.dest, None) or []
        kind = 'overlay' if option_string in ('-o', '--overlay') else 'patch'
        layers.append((kind, values))
        setattr(namespace, self.dest, layers)

def build_parser():
    p = argparse.ArgumentParser(
        prog=PROGRAM,
        description='Universal, dynamic meta-runner for CLI tools driven by '
                    'declarative JSON configurations.')
    p.add_argument('-m', '--mode', metavar='MODE',
                   help='select the mode to run')
    p.add_argument('-lm', '--list-modes', action='store_true',
                   help='list modes and exit')
    p.add_argument('-lb', '--list-bases', action='store_true',
                   help='list bases and exit')
    p.add_argument('-lo', '--list-overlays', action='store_true',
                   help='list overlays and exit')
    p.add_argument('-loa', '--list-optional-args', action='store_true',
                   help="list the resolved mode's optional arguments and exit")
    p.add_argument('-s', '--show', action='store_true',
                   help='show raw templates (dry run unless combined with -r)')
    p.add_argument('-S', '--show-resolved', action='store_true',
                   help='show resolved values (dry run unless combined with -r)')
    p.add_argument('-r', '--run', action='store_true',
                   help='force execution (overrides dry-run behavior of -s/-S)')
    p.add_argument('-t', '--time', action='store_true',
                   help='no-op (timing is enabled by default)')
    p.add_argument('-T', '--no-time', action='store_true',
                   help='do not measure/print execution time')
    p.add_argument('-c', '--config-dir', metavar='DIR',
                   help='directory containing the JSON configuration files')
    p.add_argument('-q', '--quiet', action='store_true',
                   help='suppress [WARNING] and [INFO] messages')
    p.add_argument('-y', '--yes', '--no-confirm', action='store_true',
                   help='skip the confirmation prompt')
    p.add_argument('-o', '--overlay', dest='layers', action=LayerAction,
                   metavar='NAME', default=None,
                   help='apply a named overlay (repeatable)')
    p.add_argument('-p', '--patch', dest='layers', action=LayerAction,
                   metavar='PATH', default=None,
                   help='apply an anonymous patch (repeatable)')
    p.add_argument('--strict-overlays', action='store_true',
                   help='fail on overlay_for incompatibility')
    p.add_argument('--assume-trusted', action='store_true',
                   help='skip interactive dynamic-content trust prompts')
    p.add_argument('overrides', nargs='*', metavar='OVERRIDES',
                   help="tokens after '--': variable/argument overrides and positionals")
    return p

def choose_config_dir(cli_dir):
    if cli_dir:
        d = cli_dir
    else:
        env_dir = os.environ.get('MRUN_CONFIG_DIR')
        if env_dir:                                   # an empty value is ignored
            d = env_dir
        else:
            d = str(Path(__file__).parent / 'conf')
    return Path(os.path.expanduser(d)).resolve()

# --------------------------------------------------------------------------- main
def run(argv):
    global _quiet
    parser = build_parser()
    args = parser.parse_args(argv)
    _quiet = args.quiet
    assume_yes = args.yes or os.environ.get('MRUN_YES') == '1'
    assume_trusted = args.assume_trusted or os.environ.get('MRUN_ASSUME_TRUSTED') == '1'
    space = ConfigSpace(choose_config_dir(args.config_dir))
    space.scan()
    # Listings: first of -lm > -lb > -lo wins; no -m required; no trust gate.
    if args.list_modes:
        print_listing(space, 'mode')
        return 0
    if args.list_bases:
        print_listing(space, 'base')
        return 0
    if args.list_overlays:
        print_listing(space, 'overlay')
        return 0
    if args.mode is None:
        parser.error('the following arguments are required: -m/--mode')
    var_overrides, flag_overrides, positionals = parse_overrides(args.overrides or [])
    on_load = lambda p: info(f"Loading config: {p}")
    seen = set()
    mode_path = resolve_mode(space, args.mode)
    mode_chain = build_chain(space, mode_path, seen, on_load=on_load)
    layers = list(mode_chain)
    for kind, value in (args.layers or []):
        if kind == 'overlay':
            path, cfg = resolve_overlay(space, value)
            check_overlay_compat(space, cfg, mode_chain, args.strict_overlays)
            info(f"Applying overlay: {path}")
            try:
                layers.extend(build_chain(space, path, seen, on_load=on_load))
            except MrunError as exc:
                raise MrunError(f"Error resolving overlay '{value}': {exc}")
        else:
            path = resolve_patch(space, value)
            rp, cfg = space.load(path)
            if not isinstance(cfg, dict):
                raise MrunError(f"Error resolving patch '{value}': patch must be a JSON object")
            info(f"Applying patch: {path}")
            try:
                layers.extend(build_chain(space, path, seen, on_load=on_load))
            except MrunError as exc:
                raise MrunError(f"Error resolving patch '{value}': {exc}")
    trust_gate(space, layers, assume_trusted)
    state = evaluate(space, layers, var_overrides, flag_overrides, positionals)
    if args.list_optional_args:
        print_loa(state, state.run_name or args.mode)
        return 0
    if not state.has_executable or not state.executable:
        raise MrunError("No executable defined in configuration")
    cmd, cmd_str = build_command(state)
    show_raw = args.show
    show_resolved = args.show_resolved
    if show_raw or show_resolved:
        if show_raw and show_resolved:
            print_dry_run(state, resolved=False, cmd_str=cmd_str, print_command=False)
            print()
            print_dry_run(state, resolved=True, cmd_str=cmd_str, print_command=True)
        elif show_raw:
            print_dry_run(state, resolved=False, cmd_str=cmd_str)
        else:
            print_dry_run(state, resolved=True, cmd_str=cmd_str)
        if not args.run:
            return 0
    return execute(state, cmd, cmd_str, args.no_time, assume_yes)

def main():
    try:
        return run(sys.argv[1:])
    except MrunError as exc:
        error(str(exc))
        return 1
    except BrokenPipeError:
        return 1

if __name__ == '__main__':
    sys.exit(main())

