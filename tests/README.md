# Test Requirements Files

This folder contains test-specific requirements files optimized for different testing scenarios.

## Files

### `requirements-ci.txt` (~500MB)
**Minimal dependencies for fast unit tests**

Used by CI "Quick Tests" job for fast validation with mocked dependencies.

**Install:**
```bash
pip install -r tests/requirements-ci.txt
```

**Run:**
```bash
pytest tests/ -v -m "not slow"
```

### `requirements-integration.txt` (~1.5GB)
**CI-optimized dependencies for integration tests**

Used by CI "Full Integration" job. Includes all functional dependencies but uses CPU-only packages to save disk space.

**Differences from production `requirements.txt`:**
- ✅ `onnxruntime` (CPU) instead of `onnxruntime-gpu`
- ✅ `opencv-python-headless` instead of `opencv-python`
- ❌ No Jupyter/JupyterLab
- ❌ No Streamlit

**Install:**
```bash
pip install -r tests/requirements-integration.txt
```

**Run:**
```bash
pytest tests/ -v
```

## For Local Testing

For local development and testing, use the root `requirements.txt` which includes GPU support and all dev tools:

```bash
pip install -r requirements.txt
./scripts/run-tests.sh
```

## See Also

- [`pytest.ini`](pytest.ini) - Pytest configuration (markers, warning filters)
