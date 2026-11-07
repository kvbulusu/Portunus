"""Static Tcl checks shared by the read-only probe tests (§219a).

Generalises the §218 bbox check (soc_cts_probe.tcl crashed with rc=2 on
`format "... x[%s..%s] ..."`, Tcl ran `%s..%s` as a command):
  - write_forbidden(): db/def/verilog/... writers on code lines
  - balance(): braces / brackets balanced, ignoring comments and escapes
  - bracket_issues(): unescaped [ ] inside the quoted string argument of
    format / puts / out / trow / hdr. In a format string every [ must be
    escaped; in the others a [ must start a real command substitution
    (a proc defined in the script, a Tcl builtin, a known OpenROAD/STA
    command, or $obj method), never a bus index like data_o[12] or [%s].
"""
import re

WRITERS = ("write_db", "orfs_write_db", "write_def", "write_verilog", "write_spef",
           "write_sdc", "write_odb", "write_abstract_lef", "write_timing_model",
           "write_lef", "write_cdl", "write_gds")

TCL_BUILTINS = set("""
after append apply array binary break catch cd chan clock close concat continue dict
encoding eof error eval exec exit expr fblocked fconfigure fcopy file fileevent flush
for foreach format gets glob global if incr info interp join lappend lassign lindex
linsert list llength lmap lrange lrepeat lreplace lreverse lsearch lset lsort namespace
open package pid proc puts pwd read regexp regsub rename return scan seek set socket
source split string subst switch tell time trace unset update uplevel upvar variable
vwait while tcl::mathop::+ tcl::mathfunc::min tcl::mathfunc::max
""".split())

STA_CMDS = set("""
get_property get_full_name get_name find_timing_paths get_pins get_cells get_nets
get_ports all_clocks report_checks report_worst_slack report_tns report_clock_latency
report_clock_skew report_check_types ord::get_db_block sta::find_pin
sta::with_output_to_variable get_lib_cells all_registers
""".split())

QUOTED_CMDS = ("format", "puts", "out", "trow", "hdr")


def code_lines(src):
    """(lineno, line) for non-comment lines."""
    for i, line in enumerate(src.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        yield i, line


def write_forbidden(src):
    pat = re.compile(r"\b(%s)\b" % "|".join(WRITERS))
    return [(i, l) for i, l in code_lines(src) if pat.search(l)]


def balance(src):
    """Return (braces, brackets) depth at EOF; both must be 0. Also
    returns the first line where a depth went negative (or None)."""
    br = bk = 0
    neg = None
    for i, line in code_lines(src):
        j = 0
        while j < len(line):
            c = line[j]
            if c == "\\":
                j += 2
                continue
            if c == "{":
                br += 1
            elif c == "}":
                br -= 1
            elif c == "[":
                bk += 1
            elif c == "]":
                bk -= 1
            if (br < 0 or bk < 0) and neg is None:
                neg = i
            j += 1
    return br, bk, neg


def defined_procs(src):
    return set(re.findall(r"^\s*proc\s+(\S+)", src, re.M))


def _scan_cmd_subst(s, j):
    """s[j] == '['; return index after the matching ']' (handles nesting,
    escapes, nested quotes / braces roughly)."""
    depth = 0
    k = j
    while k < len(s):
        c = s[k]
        if c == "\\":
            k += 2
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    return len(s)


def _quoted_after(line, start):
    """line[start] == '"'; return (string_body, end_index) honoring escapes
    and [..] command substitutions (which may contain quotes)."""
    j = start + 1
    body = []
    while j < len(line):
        c = line[j]
        if c == "\\":
            body.append(line[j:j + 2])
            j += 2
            continue
        if c == "[":
            e = _scan_cmd_subst(line, j)
            body.append(line[j:e])
            j = e
            continue
        if c == '"':
            return "".join(body), j
        body.append(c)
        j += 1
    return "".join(body), j


def bracket_issues(src, extra_cmds=()):
    known = TCL_BUILTINS | STA_CMDS | defined_procs(src) | set(extra_cmds)
    issues = []
    cmd_re = re.compile(r"(?:^|[\s\[;{])(%s)\s+\"" % "|".join(QUOTED_CMDS))
    for i, line in code_lines(src):
        for m in cmd_re.finditer(line):
            cmd = m.group(1)
            body, _ = _quoted_after(line, m.end() - 1)
            j = 0
            while j < len(body):
                c = body[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "]":
                    issues.append((i, cmd, "stray unescaped ]", line.strip()))
                    j += 1
                    continue
                if c == "[":
                    e = _scan_cmd_subst(body, j)
                    inner = body[j + 1:e - 1].strip()
                    word = inner.split()[0] if inner.split() else ""
                    if cmd == "format":
                        issues.append((i, cmd, "unescaped [ in format string: [%s]" % inner,
                                       line.strip()))
                    elif not (word.startswith(("$", "[")) or word in known):
                        issues.append((i, cmd, "[%s] is not a command" % inner, line.strip()))
                    j = e
                    continue
                j += 1
    return issues
