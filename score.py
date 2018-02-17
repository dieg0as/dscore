#!/usr/bin/env python
"""Score diarization system output.

To evaluate system output stored in RTTM files ``sys1.rttm``, ``sys2.rttm``,
... against a corresponding reference diarization stored in RTTM files
``ref1.rttm``, ``ref2.rttm``, ...:

    python score.py -r ref1.rttm ref2.rttm ... -s sys1.rttm sys2.rttm ...

which will calculate and report the following metrics both overall and on
a per-file basis:

- diarization error rate (DER)
- B-cubed precision (B3-Precision)
- B-cubed recall (B3-Recall)
- B-cubed F1 (B3-F1)
- Goodman-Kruskal tau in the direction of the reference diarization to the
  system diarization (GKT(ref, sys))
- Goodman-Kruskal tau in the direction of the system diarization to the
  reference diarization (GKT(sys, ref))
- conditional entropy of the reference diarization given the system
  diarization in bits (H(ref|sys))
- conditional entropy of the system diarization given the reference
  diarization in bits (H(sys|ref))
- mutual information in bits (MI)
- normalized mutual information (NMI)

Alternately, we could have specified the reference and system RTTM files via
script files of paths (one per line) using the ``-R`` and ``-S`` flags:

    python score.py -R ref.scp -S sys.scp

By default the scoring regions for each file will be determined automatically
from the reference and speaker turns. However, it is possible to specify
explicit scoring regions using a NIST un-partitioned evaluation map (UEM) file
and the ``-u`` flag. For instance, the following:

    python score.py -u all.uem -R ref.scp -S sys.scp

will load the files to be scored + scoring regions from ``all.uem``, filter out
and warn about any speaker turns not present in those files, and trim the
remaining turns to the relevant scoring regions before computing the metrics
as before.

Diarization error rate (DER) is scored using the NIST ``md-eval.pl`` tool with
a default collar size of 0 ms and explicitly including regions that contain
overlapping speech in the reference diarization. If desired, this behavior
can be altered using the ``--collar`` and ``--ignore_overlaps`` flags. For
instance

    python score.py --collar 0.100 --ignore_overlaps -R ref.scp -S sys.scp

would compute DER using a 100 ms collar and with overlapped speech ignored.
All other metrics are computed off of frame-level labelings generated from the
reference and system speaker turns **WITHOUT** any use of collars. The default
frame step is 10 ms, which may be altered via the ``--step`` flag. For more
details, consult the docstrings within the ``scorelib.metrics`` module.

The overall and per-file results will be printed to STDOUT as a table formatted
using the ``tabulate`` package. Some basic control of the formatting of this
table is possible via the ``--n_digits`` and ``--table_format`` flags. The
former controls the number of decimal places printed for floating point
numbers, while the latter controls the table format. For a list of valid
table formats plus example outputs, consult the documentation for the
``tabulate`` package:

    https://pypi.python.org/pypi/tabulate
"""
from __future__ import print_function
from __future__ import unicode_literals
import argparse
import os
import sys

from tabulate import tabulate

from scorelib import __version__ as VERSION
from scorelib.argparse import ArgumentParser
from scorelib.rttm import load_rttm
from scorelib.turn import merge_turns, trim_turns
from scorelib.score import score
from scorelib.six import iterkeys
from scorelib.uem import gen_uem, load_uem
from scorelib.utils import error, info, warn, xor


class RefRTTMAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        if not xor(namespace.ref_rttm_fns, namespace.ref_rttm_scpf):
            parser.error('Exactly one of -r and -R must be set.')


class SysRTTMAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        if not xor(namespace.sys_rttm_fns, namespace.sys_rttm_scpf):
            parser.error('Exactly one of -s and -S must be set.')


def load_rttms(rttm_fns):
    """Load speaker turns from RTTM files.

    Parameters
    ----------
    rttm_fns : list of str
        Paths to RTTM files.

    Returns
    -------
    turns : list of Turn
        Speaker turns.

    file_ids : set
        File ids found in ``rttm_fns``.
    """
    turns = []
    file_ids = set()
    file_spks = {}
    for rttm_fn in rttm_fns:
        if not os.path.exists(rttm_fn):
            error('Unable to open RTTM file: %s' % rttm_fn)
            sys.exit(1)
        try:
            turns_, speaker_ids_, file_ids_ = load_rttm(rttm_fn)
            turns.extend(turns_)
            file_ids.update(file_ids_)
            file_spks.update({list(file_ids_)[0]:len(speaker_ids_)})
        except IOError as e:
            error('Invalid RTTM file: %s. %s' % (rttm_fn, e))
            sys.exit(1)
    return turns, file_ids, file_spks


def check_for_empty_files(ref_turns, sys_turns, uem):
    """Warn on files in UEM without reference or speaker turns."""
    ref_file_ids = set([turn.file_id for turn in ref_turns])
    sys_file_ids = set([turn.file_id for turn in sys_turns])
    for file_id in sorted(iterkeys(uem)):
        if file_id not in ref_file_ids:
            warn('File "%s" missing in reference RTTMs.' % file_id)
        if file_id not in sys_file_ids:
            warn('File "%s" missing in system RTTMs.' % file_id)
    # TODO: Clarify below warnings; this indicates that there are no
    #       ELIGIBLE reference/system turns.
    if not ref_turns:
        warn('No reference speaker turns found within UEM scoring regions.')
    if not sys_turns:
        warn('No system speaker turns found within UEM scoring regions.')


def load_script_file(fn):
    """Load file names from ``fn``."""
    with open(fn, 'rb') as f:
        return [line.decode('utf-8').strip() for line in f]


def print_table(file_to_scores, global_scores, ref_spks_len, sys_spks_len,
                n_digits=2, table_format='simple'):
    """Pretty print scores as table.

    Parameters
    ----------
    file_to_scores : dict
        Mapping from file ids in ``uem`` to ``Scores`` instances.

    global_scores : Scores
        Global scores.

    n_digits : int, optional
        Number of decimal digits to display.
        (Default: 3)

    table_format : str, optional
        Table format. Passed to ``tabulate.tabulate``.
        (Default: 'simple')
    """
    col_names = ['File',
                 'DER', # Diarization error rate.
                 'B3-Precision', # B-cubed precision.
                 'B3-Recall', # B-cubed recall.
                 'B3-F1', # B-cubed F1.
                 'GKT(ref, sys)', # Goodman-Krustal tau (ref, sys).
                 'GKT(sys, ref)', # Goodman-Kruskal tau (sys, ref).
                 'H(ref|sys)',  # Conditional entropy of ref given sys.
                 'H(sys|ref)',  # Conditional entropy of sys given ref.
                 'MI', # Mutual information.
                 'NMI', # Normalized mutual information.
                 '#r', # Speakers number of reference.
                 '#s', # Speakers number of system.
                 's-r', # Speakers number diff (System - Reference)
                ]
    rows = []
    matched_spks_files = 0
    for file_id in sorted(iterkeys(file_to_scores)):
        scores = file_to_scores[file_id]
        diff_spks = sys_spks_len[file_id] - ref_spks_len[file_id]
        if diff_spks != 0:
            matched_spks_files += 1
        row = [file_id, scores.der, scores.bcubed_precision,
               scores.bcubed_recall, scores.bcubed_f1, scores.tau_ref_sys,
               scores.tau_sys_ref, scores.ce_ref_sys, scores.ce_sys_ref,
               scores.mi, scores.nmi,
               ref_spks_len[file_id], sys_spks_len[file_id], diff_spks]
        rows.append(row)
    rows.append(['OVERALL', global_scores.der, global_scores.bcubed_precision,
                 global_scores.bcubed_recall, global_scores.bcubed_f1,
                 global_scores.tau_ref_sys, global_scores.tau_sys_ref,
                 global_scores.ce_ref_sys, global_scores.ce_sys_ref,
                 global_scores.mi, global_scores.nmi, '-', '-', matched_spks_files])
    floatfmt = '.%df' % n_digits
    tbl = tabulate(
        rows, headers=col_names, floatfmt=floatfmt, tablefmt=table_format)
    print(tbl)



if __name__ == '__main__':
    # Parse command line arguments.
    parser = ArgumentParser(
        description='Score diarization from RTTM files.', add_help=True,
        usage='%(prog)s [options]')
    parser.add_argument(
        '-r', nargs='+', default=[], metavar='STR', dest='ref_rttm_fns',
        action=RefRTTMAction,
        help='reference RTTM files (default: %(default)s)')
    parser.add_argument(
        '-R', nargs=None, metavar='STR', dest='ref_rttm_scpf',
        action=RefRTTMAction,
        help='reference RTTM script file (default: %(default)s)')
    parser.add_argument(
        '-s', nargs='+', default=[], metavar='STR', dest='sys_rttm_fns',
        action=SysRTTMAction,
        help='system RTTM files (default: %(default)s)')
    parser.add_argument(
        '-S', nargs=None, metavar='STR', dest='sys_rttm_scpf',
        action=SysRTTMAction,
        help='system RTTM script file (default: %(default)s)')
    parser.add_argument(
        '-u,--uem', nargs=None, metavar='STR', dest='uemf',
        help='un-partitioned evaluation map file (default: %(default)s)')
    parser.add_argument(
        '--collar', nargs=None, default=0.0, type=float, metavar='FLOAT',
        help='collar size in seconds for DER computaton '
             '(default: %(default)s)')
    parser.add_argument(
        '--ignore_overlaps', action='store_true', default=False,
        help='ignore overlaps when computing DER')
    parser.add_argument(
        '--step', nargs=None, default=0.010, type=float, metavar='FLOAT',
        help='step size in seconds (default: %(default)s)')
    parser.add_argument(
        '--n_digits', nargs=None, default=2, type=int, metavar='INT',
        help='number of decimal places to print (default: %(default)s)')
    parser.add_argument(
        '--table_fmt', nargs=None, dest='table_format', default='simple',
        metavar='STR',
        help='tabulate table format (default: %(default)s)')
    parser.add_argument(
        '--version', action='version',
        version='%(prog)s ' + VERSION)
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)
    args = parser.parse_args()

    # Check that at least one reference RTTM and at least one system RTTM
    # was specified.
    if args.ref_rttm_scpf is not None:
        args.ref_rttm_fns = load_script_file(args.ref_rttm_scpf)
    if args.sys_rttm_scpf is not None:
        args.sys_rttm_fns = load_script_file(args.sys_rttm_scpf)
    if len(args.ref_rttm_fns) < 1:
        error('No reference RTTMs specified.')
        sys.exit(1)
    if len(args.sys_rttm_fns) < 1:
        error('No system RTTMs specified.')
        sys.exit(1)

    # Load speaker/reference speaker turns and UEM. If no UEM specified,
    # determine it automatically.
    info('Loading speaker turns from reference RTTMs...', file=sys.stderr)
    ref_turns, ref_file_ids, ref_spks_len = load_rttms(args.ref_rttm_fns)
    info('Loading speaker turns from system RTTMs...', file=sys.stderr)
    sys_turns, sys_file_ids, sys_spks_len = load_rttms(args.sys_rttm_fns)
    if args.uemf is not None:
        info('Loading universal evaluation map...', file=sys.stderr)
        uem = load_uem(args.uemf)
    else:
        warn('No universal evaluation map specified. Approximating from '
             'reference and speaker turn extents...')
        uem = gen_uem(ref_turns, sys_turns)

    # Trim turns to UEM scoring regions and merge any that overlap.
    info('Trimming reference speaker turns to UEM scoring regions...',
         file=sys.stderr)
    ref_turns = trim_turns(ref_turns, uem)
    info('Trimming system speaker turns to UEM scoring regions...',
         file=sys.stderr)
    sys_turns = trim_turns(sys_turns, uem)
    info('Checking for overlapping reference speaker turns...',
         file=sys.stderr)
    ref_turns = merge_turns(ref_turns)
    info('Checking for overlapping system speaker turns...',
         file=sys.stderr)
    sys_turns = merge_turns(sys_turns)

    # Score.
    check_for_empty_files(ref_turns, sys_turns, uem)
    file_to_scores, global_scores = score(
        ref_turns, sys_turns, uem, args.collar, args.ignore_overlaps,
        args.step)
    print_table(file_to_scores, global_scores, ref_spks_len, sys_spks_len,
                args.n_digits, args.table_format)
