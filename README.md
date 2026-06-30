# ci-inspector

Catalog Item infrastructure inspector for Babylon/AgnosticV.

Extracts and analyzes infrastructure requirements from CatalogItem definitions, validates against policy thresholds, and generates reports for capacity planning and cost estimation.

## Features

- **Multi-source discovery**: Read from Git repos or live Kubernetes clusters
- **Pattern-based extraction**: Automatically detects AWS, Azure, GCP, CNV, ROSA, and OCP pool resources
- **Binder lab support**: Recursively discovers linked components and shared pools
- **Threshold policies**: Cascading YAML-based rules for vCPU, memory, cost, and instance type validation
- **Cost estimation**: AWS/Azure/GCP pricing via cloud APIs, CNV formula-based pricing
- **Multiple output formats**: JSON, table, GitHub Actions annotations, Google Sheets
- **GitHub Actions integration**: Automatically check changed catalog items in PRs

## Installation

### From PyPI

```bash
pip install ci-inspector
```

### From source

```bash
git clone https://github.com/redhat-cop/ci-inspector.git
cd ci-inspector
pip install -e .
```

### Optional dependencies

```bash
# For Kubernetes source
pip install ci-inspector[k8s]

# For Google Sheets output
pip install ci-inspector[sheets]

# For development (pytest, ruff, etc.)
pip install ci-inspector[dev]

# All optional dependencies
pip install ci-inspector[all]
```

## Quick Start

### Analyze a catalog item

```bash
# From local Git repo
ci-inspector analyze my-workshop --repo-path /path/to/agnosticv

# From Kubernetes cluster
ci-inspector analyze my-workshop --source k8s --kubeconfig ~/.kube/config

# JSON output
ci-inspector analyze my-workshop --format json

# Include cost estimates
ci-inspector analyze my-workshop --pricing
```

### Check against policies

```bash
# Check a single catalog item
ci-inspector check my-workshop --repo-path /path/to/agnosticv

# Check all catalog items
ci-inspector check-all --repo-path /path/to/agnosticv

# Custom config location
ci-inspector check my-workshop --config /path/to/.ci-inspector.yml

# GitHub Actions annotations format
ci-inspector check my-workshop --format annotations

# Fail on warnings (default: fail on errors only)
ci-inspector check my-workshop --fail-on warning
```

### Detect changed catalog items

```bash
# Detect what changed in a PR
ci-inspector detect-changes --base origin/main

# Output:
# my-workshop
# another-lab
```

### Scan all catalog items

```bash
# JSON output
ci-inspector scan --format json

# Table output
ci-inspector scan --format table

# Google Sheets output
ci-inspector scan --format sheets --sheet-id <spreadsheet-id> --credentials /path/to/sa-key.json

# Filter by glob pattern
ci-inspector scan --filter "ocp*" --format json

# Include pricing
ci-inspector scan --pricing

# Prune removed CIs from sheet
ci-inspector scan --format sheets --sheet-id <id> --credentials <key> --prune
```

## Configuration

### Threshold policies

Create `.ci-inspector.yml` files in your repo for cascading threshold policies:

```yaml
# Root-level: /repo/.ci-inspector.yml
thresholds:
  max_vcpus: 64
  max_memory_gb: 256
  max_cost_per_hour: 10.00
  flagged_instance_types:
    - pattern: "p3.*"
      severity: warning
      message: "GPU instance requires approval"
    - pattern: "metal-.*"
      severity: error
      message: "Bare metal instances not allowed"
  
  # Per-cloud overrides
  per_cloud:
    AWS:
      max_vcpus: 128
    CNV:
      max_vcpus: 256

# Optional: disable thresholds for specific catalog items
ignore:
  - "tests/**"
  - "legacy-labs/*"

# Optional: pricing config
pricing:
  cnv:
    cpu_per_core_per_hour: 0.0711
    memory_per_gb_per_hour: 0.00175
    storage_per_gb_per_hour: 0.00012
```

**Cascading rules**: Configs at `/my-workshop/.ci-inspector.yml` override `/summit/.ci-inspector.yml`, which overrides `/.ci-inspector.yml`.

**Disabling thresholds**: Set `thresholds: null` in a more specific config to disable checks for that catalog item.

### Flagged instance types

Use glob patterns to flag or block specific instance types:

```yaml
thresholds:
  flagged_instance_types:
    # Warning: requires manual approval
    - pattern: "p3.*"
      severity: warning
      message: "GPU instance - ensure quota available"
    
    # Error: blocked entirely
    - pattern: "x1e.*"
      severity: error
      message: "High-memory instances require VP approval"
    
    # Multiple patterns
    - pattern: "m5*.metal"
      severity: error
      message: "Bare metal instances not supported"
```

## GitHub Actions

### Automatic PR checks

Add to `.github/workflows/ci-inspector.yml`:

```yaml
name: CI Inspector

on:
  pull_request:
    paths:
      - '**/*.yml'
      - '**/*.yaml'

permissions:
  pull-requests: write

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # Need full history for detect-changes

      - uses: redhat-cop/ci-inspector@v1
        with:
          source: git
          fail-on: error  # or "warning"
```

### Advanced configuration

```yaml
- uses: redhat-cop/ci-inspector@v1
  with:
    source: git
    config: .ci-inspector.yml  # Override cascading lookup
    fail-on: warning
    pricing: true
    python-version: '3.12'
```

### Kubernetes source in GitHub Actions

```yaml
- uses: redhat-cop/ci-inspector@v1
  with:
    source: k8s
  env:
    KUBECONFIG: ${{ secrets.KUBECONFIG_CONTENT }}
```

## CLI Reference

### `ci-inspector analyze`

Analyze infrastructure for a catalog item.

**Arguments:**
- `CI_NAME`: Catalog item name (required)

**Options:**
- `--stage TEXT`: Stage (prod, dev, event) [default: prod]
- `--source [git|k8s]`: Data source [default: git]
- `--repo-path TEXT`: Path to AgnosticV repo checkout [default: .]
- `--kubeconfig TEXT`: Kubeconfig path (required for k8s source)
- `--format [json|table]`: Output format [default: table]
- `--pricing / --no-pricing`: Include cost estimates [default: no-pricing]

**Examples:**
```bash
ci-inspector analyze my-workshop
ci-inspector analyze my-workshop --stage dev
ci-inspector analyze my-workshop --format json --pricing
ci-inspector analyze my-workshop --source k8s --kubeconfig ~/.kube/config
```

### `ci-inspector check`

Check a catalog item against threshold policies.

**Arguments:**
- `CI_NAME`: Catalog item name (required)

**Options:**
- `--stage TEXT`: Stage [default: prod]
- `--source [git|k8s]`: Data source [default: git]
- `--repo-path TEXT`: Path to AgnosticV repo [default: .]
- `--kubeconfig TEXT`: Kubeconfig path
- `--config TEXT`: Override config path (cascading lookup by default)
- `--format [json|table|annotations]`: Output format [default: table]
- `--fail-on [warning|error]`: Fail on severity level [default: error]

**Examples:**
```bash
ci-inspector check my-workshop
ci-inspector check my-workshop --fail-on warning
ci-inspector check my-workshop --format annotations
ci-inspector check my-workshop --config /custom/.ci-inspector.yml
```

### `ci-inspector check-all`

Check all catalog items against threshold policies.

**Options:**
- `--source [git|k8s]`: Data source [default: git]
- `--repo-path TEXT`: Path to AgnosticV repo [default: .]
- `--kubeconfig TEXT`: Kubeconfig path
- `--format [json|table|annotations]`: Output format [default: table]
- `--fail-on [warning|error]`: Fail on severity level [default: error]

**Examples:**
```bash
ci-inspector check-all
ci-inspector check-all --fail-on warning
ci-inspector check-all --format annotations
```

**Exit codes:**
- `0`: All checks passed
- `1`: At least one violation found

### `ci-inspector detect-changes`

Detect changed catalog items between git refs (for CI/CD).

**Options:**
- `--base TEXT`: Base git ref to compare against (required)

**Examples:**
```bash
ci-inspector detect-changes --base origin/main
ci-inspector detect-changes --base HEAD~1
```

**Output:**
```
my-workshop
another-lab
```

### `ci-inspector scan`

Scan all catalog items (bulk discovery).

**Options:**
- `--source [git|k8s]`: Data source [default: git]
- `--repo-path TEXT`: Path to AgnosticV repo [default: .]
- `--kubeconfig TEXT`: Kubeconfig path
- `--format [json|table|sheets]`: Output format [default: json]
- `--sheet-id TEXT`: Google Sheets spreadsheet ID (required for sheets format)
- `--credentials TEXT`: Google SA key path (required for sheets format)
- `--pricing / --no-pricing`: Include cost estimates [default: no-pricing]
- `--filter TEXT`: Filter by CI name glob (e.g., `ocp*`, `*-prod`)
- `--prune / --no-prune`: Hard-delete removed CIs from sheet [default: no-prune]

**Examples:**
```bash
ci-inspector scan --format json
ci-inspector scan --format table --filter "ocp*"
ci-inspector scan --format sheets --sheet-id 1A2B3C --credentials sa-key.json
ci-inspector scan --pricing --filter "summit/*"
```

## Architecture

### Discovery pipeline

1. **Fetch catalog item**: Retrieve from Git repo or K8s cluster
2. **Extract metadata**: Parse `__meta__.deployer.scm_url`, `env_type`, `cloud_provider`
3. **Read AgnosticD defaults**: Merge base and cloud-specific defaults from agnosticd/agnosticd-v2
4. **Detect binder pattern**: Check for `__meta__.components` without own instances
5. **Extract instances**: Apply pattern-based extraction for all cloud types
6. **Extract CNV workload VMs**: Parse `cnv_instances` for CNV-hosted VMs
7. **Resolve linked components**: Recursively discover binder sub-components
8. **Return result**: DiscoveryResult with components, totals, warnings, errors

### Extraction patterns

- **Pattern A**: Cloud instances (`cloud_instances`, `instances`)
- **Pattern B**: Role variables (`bastion_instance_type`, `worker_instance_count`)
- **Pattern C**: CNV instances (`cnv_instances`)
- **Pattern D**: OCP pool clusters (`__meta__.components` → `ocp-cluster-cnv-pools`)
- **Pattern E**: ROSA clusters (`rosa_cluster_size`, `rosa_machine_type`, `rosa_compute_nodes`)

### Supported clouds

- AWS (EC2)
- Azure
- GCP (Google Cloud)
- CNV (OpenShift Virtualization)
- ROSA (Red Hat OpenShift Service on AWS)
- OCP Pools (shared cluster pools)

## Development

### Setup

```bash
git clone https://github.com/redhat-cop/ci-inspector.git
cd ci-inspector
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Run tests

```bash
# All tests
pytest tests/ -v

# Specific test file
pytest tests/test_integration.py -v

# With coverage
pytest tests/ --cov=ci_inspector --cov-report=term-missing
```

### Linting

```bash
# Check
ruff check src/ tests/

# Auto-fix
ruff check src/ tests/ --fix
```

### Test fixtures

- `tests/fixtures/agnosticv/`: Mock AgnosticV repo with sample catalog items
- `tests/fixtures/agnosticd/`: AgnosticD defaults fixtures
- `tests/fixtures/configs/`: Cascading `.ci-inspector.yml` examples
- `tests/fixtures/discovery/`: YAML fixtures for each extraction pattern

## Examples

### Full analysis with pricing

```bash
ci-inspector analyze summit-ocp-workshop --pricing --format json > analysis.json
```

### Batch validation with warnings

```bash
ci-inspector check-all --fail-on warning --format table
```

### CI/CD integration

```bash
#!/bin/bash
BASE_SHA="${GITHUB_EVENT_PULL_REQUEST_BASE_SHA}"
CHANGED=$(ci-inspector detect-changes --base "$BASE_SHA")

EXIT_CODE=0
for CI_NAME in $CHANGED; do
  echo "Checking $CI_NAME..."
  ci-inspector check "$CI_NAME" --format annotations || EXIT_CODE=$?
done

exit $EXIT_CODE
```

### Google Sheets export

```bash
ci-inspector scan \
  --format sheets \
  --sheet-id 1A2B3C4D5E6F7G8H9I0J \
  --credentials /path/to/sa-key.json \
  --pricing \
  --filter "summit/*" \
  --prune
```

## Troubleshooting

### "Could not find catalog item"

- Verify the catalog item exists in the repo or cluster
- Check the correct `--stage` (prod, dev, event)
- For K8s source, verify kubeconfig and namespace access

### "No pricing found for instance type"

- AWS: Pricing API may be unavailable or instance type deprecated
- Azure/GCP: Requires live API access (no offline fallback)
- CNV: Uses formula-based pricing (always available)

### "Threshold violations not detected"

- Check `.ci-inspector.yml` is in the expected location
- Verify cascading config hierarchy (repo root → group → catalog item)
- Ensure `thresholds:` is not set to `null` (which disables checks)

### GitHub Action shows no changed items

- Verify `fetch-depth: 0` in checkout step (required for git history)
- Check that PR has a valid base SHA
- Ensure workflow triggers on YAML file changes

## License

Apache-2.0

## Contributing

Contributions welcome! Please open an issue or PR on GitHub.

1. Fork the repo
2. Create a feature branch
3. Make your changes
4. Run tests: `pytest tests/ -v`
5. Run linting: `ruff check src/ tests/`
6. Submit a PR

## Support

- GitHub Issues: https://github.com/redhat-cop/ci-inspector/issues
- Documentation: https://github.com/redhat-cop/ci-inspector/blob/main/README.md
