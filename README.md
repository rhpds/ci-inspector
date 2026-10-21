# ci-inspector

Catalog Item infrastructure inspector for Babylon/AgnosticV. Extracts infrastructure requirements from CatalogItem definitions, checks them against policy thresholds, and reports cost estimates.

## What It Does

Given a Babylon catalog item, ci-inspector tells you exactly what it provisions:

```
$ ci-inspector analyze summit-2026/lb1404-vmware-migration-cnv --stage event --source k8s --pricing

 lb1404-vmware-migration-cnv (event) — ocp4-cluster / openshift_cnv
┏━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━┳━━━━━━━━┳━━━━━━━━━━┓
┃ Purpose       ┃ Instance Type ┃ Count ┃ vCPU ┃ Mem GB ┃  Cost/hr ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━╇━━━━━━━━╇━━━━━━━━━━┩
│ bastion       │ 2_core_4Gi    │     1 │    2 │      4 │    $0.21 │
│ compute01     │ 16_core_16Gi  │     1 │   16 │     16 │    $1.61 │
│ nfsserver     │ 4_core_4Gi    │     1 │    4 │      4 │    $0.40 │
│ control-plane │ 16_core_32Gi  │     3 │   48 │     96 │    $1.67 │
│ Totals        │               │       │   70 │    120 │ $7.22/hr │
└───────────────┴───────────────┴───────┴──────┴────────┴──────────┘
```

It works three ways:
- **From the Babylon cluster** (`--source k8s`) — fetches CatalogItem and AVC CRDs live
- **From a merged YAML** (`analyze-merged`) — accepts output of `agnosticv --merge`, for CI pipelines
- **From a git checkout** (`--source git`) — reads AgnosticV YAML files directly

## Install

```bash
pip install git+https://github.com/rhpds/ci-inspector.git
```

Or run from the repo:

```bash
git clone https://github.com/rhpds/ci-inspector.git
cd ci-inspector
./ci-inspector --help
```

## CLI Commands

### `analyze` — What does this CI provision?

```bash
# From Babylon cluster
ci-inspector analyze <ci-name> --stage event --source k8s \
  --kubeconfig ~/secrets/ocp-us-west-2.kubeconfig --pricing

# From git checkout
ci-inspector analyze <ci-name> --stage prod --source git --repo-path ~/agnosticv

# Output as JSON
ci-inspector analyze <ci-name> --source k8s --format json

# Select the CNV branch of a published multi-cloud CI (defaults to AWS)
ci-inspector analyze published.ocp4-cluster --stage prod --source k8s \
  --kubeconfig ~/secrets/ocp-us-west-2.infra.open.redhat.com.kubeconfig \
  --parameter ocp_cloud_provider=cnv --parameter cluster_size=sno --pricing
```

Repeat `--parameter NAME=VALUE` to select catalog options or override sizing.
Values accept JSON scalars (numbers, booleans, null, or quoted strings) and
plain strings. Explicit values take precedence over catalog defaults and are
routed to child components according to the catalog definition. Results describe
the selected configuration, not the configuration of historical requests.
Unresolved binder infrastructure produces warnings; child lookup errors propagate
to the parent and cause `analyze` to fail.

### `analyze-merged` — Analyze pre-merged YAML

For CI pipelines where `agnosticv --merge` has already produced the merged config:

```bash
# From a file
agnosticv --git=false --merge summit-2026/my-lab/event.yaml | sed '/^#/d' > /tmp/merged.yaml
ci-inspector analyze-merged /tmp/merged.yaml --ci-name my-lab --stage event --pricing

# From stdin
agnosticv --git=false --merge summit-2026/my-lab/event.yaml | sed '/^#/d' \
  | ci-inspector analyze-merged - --ci-name my-lab --stage event --format table
```

### `validate-merged` — Informational size + threshold check

Shows infrastructure size summary and threshold warnings **without failing the pipeline** — always exits 0. Designed for gradual rollout before enforcing hard limits with `check-merged`.

When multiple files are given, **identical stages are grouped** — if dev/event have the same infrastructure but prod differs, you see dev/event once and prod separately.

```bash
# Single file (from stdin or path)
agnosticv --git=false --merge summit-2026/my-lab/event.yaml | sed '/^#/d' \
  | ci-inspector validate-merged - --ci-name my-lab

# Multiple stages — identical ones are deduplicated
ci-inspector validate-merged dev.yml event.yml prod.yml --ci-name my-lab

# With cost estimates
ci-inspector validate-merged /tmp/merged.yaml --ci-name my-lab --pricing

# GitHub Actions annotations (warnings only, never errors)
ci-inspector validate-merged /tmp/merged.yaml --ci-name my-lab --format annotations

# JSON for programmatic consumption
ci-inspector validate-merged /tmp/merged.yaml --ci-name my-lab --format json
```

Example output (all stages identical):

```
my-lab (dev, event, prod) — Summary
  48 vCPUs · 192 GB RAM · 6 instances

┌─────────┬──────────────┬───────┬──────┬────────┬─────────┐
│ Purpose │ Instance Type│ Count │ vCPU │ Mem GB │ Cost/hr │
│ ...     │              │       │      │        │         │
└─────────┴──────────────┴───────┴──────┴────────┴─────────┘

Validation:
  [!] 48 vCPUs (AWS) exceeds threshold of 32
```

### `check` / `check-merged` — Does it pass policy thresholds?

```bash
# Check against thresholds (exits 1 if errors found)
ci-inspector check <ci-name> --stage event --source k8s --format annotations

# Check a pre-merged YAML
ci-inspector check-merged /tmp/merged.yaml --ci-name my-lab --stage event
```

### `check-all` — Check every CI in a repo

```bash
ci-inspector check-all --source git --repo-path ~/agnosticv
```

### `scan` — Export all CIs

```bash
# JSON dump of all CIs
ci-inspector scan --source k8s --format json --pricing

# Sync to Google Sheets
ci-inspector scan --source k8s --format sheets --sheet-id <id> --credentials sa-key.json
```

### `detect-changes` — What CIs changed in this PR?

```bash
ci-inspector detect-changes --base origin/master
```

## Threshold Configuration

Create a `.ci-inspector.yml` in your AgnosticV repo. Configs cascade from repo root to CI directory — child overrides parent.

```yaml
thresholds:
  # severity: warning = annotate PRs, don't block
  # severity: error   = block PRs that exceed thresholds
  severity: warning

  max_vcpus: 128
  max_memory_gb: 512
  max_instances: 20
  max_cost_per_hour: 25.00
  max_storage_gb: 2000

  # Per-cloud overrides
  clouds:
    CNV:
      max_vcpus: 256
    AWS:
      max_cost_per_hour: 30.00

  # Flag specific instance types
  flagged_instance_types:
    - pattern: "p3.*"
      severity: warning
      message: "GPU instance — confirm this is intentional"
    - pattern: "*.metal"
      severity: warning
      message: "Bare metal instance — confirm this is intentional"

ignore:
  - "includes/**"
  - "EXAMPLE_ACCOUNT/**"
```

### Cascading configs

Place `.ci-inspector.yml` at any level — child configs override parent:

```
agnosticv/
├── .ci-inspector.yml              # Repo-wide defaults
├── summit-2026/
│   ├── .ci-inspector.yml          # Tighter limits for Summit
│   └── my-workshop/
│       └── .ci-inspector.yml      # This CI needs more resources
```

Merge rules:
- Scalars (max_vcpus, severity): child wins
- Lists (flagged_instance_types): child replaces entirely
- ignore: merged (union of all levels)
- `thresholds: null`: disables checking for that CI

## Pricing

All pricing data is bundled in `data/pricing.json` — no API calls needed.

- **1,072 instance types** (AWS, Azure, GCP, Equinix, IBM) with vCPU, memory, and per-region costs
- **CNV** uses formula-based pricing (CPU $0.0969/core/hr, Memory $0.00365/GB/hr)
- Override with `--pricing-json /path/to/custom.json`

Use `--pricing` flag to include cost estimates in output.

## GitHub Actions Integration

### How it works

ci-inspector integrates into the existing AgnosticV `static-tests.sh` PR pipeline:

1. The pipeline already runs `agnosticv --merge` on every changed catalog item
2. ci-inspector receives the merged YAML and extracts infrastructure requirements
3. Findings are posted as **inline PR annotations** (warnings appear directly on the diff)
4. Findings are also included in the PR comment alongside existing checks

### Setup

1. **Add ci-inspector to `.tests/requirements.txt`**:

```
ci-inspector @ git+https://github.com/rhpds/ci-inspector.git
```

2. **Add to `.tests/static-tests.sh`** after the merge loop saves merged outputs:

```bash
# After babylon_checks, run ci-inspector on merged outputs
if command -v ci-inspector &>/dev/null; then
    for merged_file in ${merged_dir}/*; do
        [ -f "$merged_file" ] || continue
        ci_name=$(...)  # extract from filename
        stage=$(...)    # extract from filename
        ci-inspector check-merged "$merged_file" \
            --ci-name "$ci_name" --stage "$stage" \
            --format annotations
    done
fi
```

3. **Add `.ci-inspector.yml`** to the repo root with your thresholds.

### PR annotation output

When a CI exceeds thresholds, developers see warnings inline on their PR diff:

```
⚠️ CI Inspector: 96 vCPUs (CNV) exceeds threshold of 128
⚠️ CI Inspector: Bare metal instance i3.metal — confirm this is intentional
```

### Rollout strategy

**Phase 1 — Informational only** (recommended starting point):

Use `validate-merged` in the CI pipeline. Authors see their CI's size and any threshold warnings, but the pipeline never fails:

```bash
ci-inspector validate-merged dev.yml event.yml prod.yml \
    --ci-name "$ci_name" \
    --format annotations
```

**Phase 2 — Warn but don't block:**

Switch to `check-merged` with `severity: warning` in `.ci-inspector.yml`:

```yaml
thresholds:
  severity: warning    # annotate PRs, don't block
```

**Phase 3 — Enforce:**

Change to `severity: error` to block PRs that exceed thresholds:

```yaml
thresholds:
  severity: error      # block PRs that exceed thresholds
```

### Standalone GitHub Action

Alternatively, use the pre-built action without modifying `static-tests.sh`:

```yaml
# .github/workflows/ci-inspector.yml
name: Infrastructure Check
on:
  pull_request:
    paths: ['**/*.yml', '**/*.yaml']

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: rhpds/ci-inspector@main
```

## Data Sources

| Source | Flag | When to use |
|--------|------|-------------|
| K8s cluster | `--source k8s --kubeconfig PATH` | Spot-checking live catalog, full binder resolution |
| Merged YAML | `analyze-merged FILE` / `validate-merged FILE...` | CI pipelines with `agnosticv --merge` |
| Git checkout | `--source git --repo-path PATH` | Offline analysis (limited — no `#include` resolution) |

## Development

```bash
git clone https://github.com/rhpds/ci-inspector.git
cd ci-inspector
pip install -e ".[dev]"
pytest tests/ -v
```
