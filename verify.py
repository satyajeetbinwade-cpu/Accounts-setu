"""CLI entry point for the verification harness.

Usage:
  # Single period
  python verify.py --client X --period YYYY-MM --recon-type GST \
      --baseline path/to/baseline.xlsx --output path/to/report.xlsx

  # With adjudicated disagreement file
  python verify.py --client X --period YYYY-MM --recon-type GST \
      --baseline path/to/baseline.xlsx --output path/to/report.xlsx \
      --adjudicated path/to/adjudicated.xlsx

  # Multi-period (comma-separated run-ids and baselines)
  python verify.py --multi \
      --run-ids 1,2,3 \
      --baselines path1.xlsx,path2.xlsx,path3.xlsx \
      --output path/to/multi_report.xlsx

  # Export disagreements for firm review
  python verify.py --export-disagreements \
      --client X --period YYYY-MM --recon-type GST \
      --baseline path/to/baseline.xlsx --output path/to/disagreements.xlsx
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import db, queries
from src.verification.report import generate_report, generate_multi_period_report
from src.verification.baseline import load_baseline
from src.verification.alignment import align
from src.verification.analysis import build_disagreements, export_for_adjudication


def _find_run_id(client: str, period: str, recon_type: str) -> int:
    """Find the most recent run for the given context."""
    runs_df = queries.list_runs(client=client, period=period, recon_type=recon_type)
    if runs_df.empty:
        raise SystemExit(
            f"No runs found for client={client!r}, period={period!r}, "
            f"recon_type={recon_type!r}. Execute a run first."
        )
    return int(runs_df["run_id"].iloc[0])


def cmd_verify(args: argparse.Namespace) -> None:
    """Single-period verification."""
    db.init_db()

    if args.run_id:
        run_id = args.run_id
    else:
        run_id = _find_run_id(args.client, args.period, args.recon_type)

    print(f"[verify] Running verification for run {run_id}")
    print(f"[verify] Baseline: {args.baseline}")
    print(f"[verify] Output:   {args.output}")

    path = generate_report(
        run_id,
        args.baseline,
        args.output,
        adjudicated_path=args.adjudicated,
    )
    print(f"\n[verify] Report written to: {path}")


def cmd_multi(args: argparse.Namespace) -> None:
    """Multi-period verification."""
    db.init_db()

    run_ids = [int(x.strip()) for x in args.run_ids.split(",")]
    baselines = [x.strip() for x in args.baselines.split(",")]

    if len(run_ids) != len(baselines):
        raise SystemExit(
            f"--run-ids has {len(run_ids)} entries but --baselines has {len(baselines)}"
        )

    adj_paths = None
    if args.adjudicated:
        adj_paths = [x.strip() if x.strip() else None
                     for x in args.adjudicated.split(",")]

    path = generate_multi_period_report(
        run_ids, baselines, args.output,
        adjudicated_paths=adj_paths,
    )
    print(f"\n[verify] Multi-period report written to: {path}")


def cmd_export_disagreements(args: argparse.Namespace) -> None:
    """Export disagreements for firm review."""
    db.init_db()

    if args.run_id:
        run_id = args.run_id
    else:
        run_id = _find_run_id(args.client, args.period, args.recon_type)

    run = queries.get_run(run_id)
    if run is None:
        raise SystemExit(f"Run {run_id} not found")

    recon_type = run["recon_type"]
    engine_df = queries.get_results(run_id)
    baseline_df, _ = load_baseline(args.baseline, recon_type)
    alignment = align(engine_df, baseline_df, recon_type)
    disagreements = build_disagreements(alignment["aligned"], recon_type)

    if disagreements.empty:
        print("[verify] No disagreements to export.")
        return

    path = export_for_adjudication(disagreements, args.output)
    print(f"[verify] Disagreements exported to: {path}")
    print(f"[verify] {len(disagreements)} disagreement(s). Fill in the 'adjudication' column and re-import.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verification harness for the Setu reconciliation PoC."
    )
    sub = parser.add_subparsers(dest="command")

    # --- verify ---
    p_verify = sub.add_parser("verify", help="Single-period verification")
    p_verify.add_argument("--client", type=str, default=None)
    p_verify.add_argument("--period", type=str, default=None)
    p_verify.add_argument("--recon-type", type=str, default=None)
    p_verify.add_argument("--run-id", type=int, default=None,
                          help="Explicit run ID (overrides client/period/recon-type lookup)")
    p_verify.add_argument("--baseline", type=str, required=True)
    p_verify.add_argument("--output", type=str, required=True)
    p_verify.add_argument("--adjudicated", type=str, default=None)

    # --- multi ---
    p_multi = sub.add_parser("multi", help="Multi-period verification")
    p_multi.add_argument("--run-ids", type=str, required=True,
                         help="Comma-separated run IDs")
    p_multi.add_argument("--baselines", type=str, required=True,
                         help="Comma-separated baseline file paths")
    p_multi.add_argument("--output", type=str, required=True)
    p_multi.add_argument("--adjudicated", type=str, default=None,
                         help="Comma-separated adjudication file paths")

    # --- export-disagreements ---
    p_export = sub.add_parser("export-disagreements",
                              help="Export disagreements for firm review")
    p_export.add_argument("--client", type=str, default=None)
    p_export.add_argument("--period", type=str, default=None)
    p_export.add_argument("--recon-type", type=str, default=None)
    p_export.add_argument("--run-id", type=int, default=None)
    p_export.add_argument("--baseline", type=str, required=True)
    p_export.add_argument("--output", type=str, required=True)

    args = parser.parse_args()

    if args.command == "verify":
        if not args.run_id and not (args.client and args.period and args.recon_type):
            parser.error("Either --run-id or all of --client/--period/--recon-type required")
        cmd_verify(args)
    elif args.command == "multi":
        cmd_multi(args)
    elif args.command == "export-disagreements":
        if not args.run_id and not (args.client and args.period and args.recon_type):
            parser.error("Either --run-id or all of --client/--period/--recon-type required")
        cmd_export_disagreements(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
