from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_REQUIREMENTS = REPO_ROOT / "requirements-api.txt"
MACOS_SETUP = REPO_ROOT / "scripts" / "setup.sh"


def _normalized_package_names(path: Path) -> set[str]:
    names: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        name = line.split("[", 1)[0]
        for sep in ("==", ">=", "<=", "~=", "!=", ">", "<"):
            if sep in name:
                name = name.split(sep, 1)[0]
                break
        names.add(name.strip().lower().replace("_", "-"))
    return names


def test_packaged_runtime_includes_pymediainfo():
    packages = _normalized_package_names(RUNTIME_REQUIREMENTS)
    assert "pymediainfo" in packages, (
        "requirements-api.txt is used to build the packaged app venv; "
        "it must include pymediainfo because the indexer uses it for video metadata extraction."
    )


def test_packaged_runtime_includes_scenedetect():
    packages = _normalized_package_names(RUNTIME_REQUIREMENTS)
    assert "scenedetect" in packages, (
        "requirements-api.txt is used to build the packaged app venv; "
        "it must include scenedetect because the indexer uses it for video shot detection."
    )


def test_packaged_runtime_omits_unused_sentence_transformers():
    packages = _normalized_package_names(RUNTIME_REQUIREMENTS)
    assert "sentence-transformers" not in packages, (
        "The packaged runtime does not import sentence-transformers today; keeping it out "
        "avoids unnecessary torch/transformers drift in end-user installers."
    )


def test_packaged_runtime_omits_dev_only_packages():
    """Anti-bloat guard: requirements-api.txt is what the bundle ships, not the
    full dev requirements file. Dev-only packages (notebooks, plotting, ML
    analysis libs, the test framework) should never be in this file — they
    pull hundreds of MB of transitive deps that end users don't need.

    Catches regressions like the pre-PR-#103 state where the bundle silently
    shipped requirements.txt (the dev file) and pulled jupyterlab, matplotlib,
    scikit-learn, faiss-cpu, ipython, pytest, etc. into every end-user install.
    """
    packages = _normalized_package_names(RUNTIME_REQUIREMENTS)
    dev_only = {
        "jupyterlab",       # notebooks
        "ipykernel",        # notebooks
        "ipywidgets",       # notebooks
        "matplotlib",       # notebook plotting
        "scikit-learn",     # notebook analysis
        "faiss-cpu",        # porter-only (ADR-010: SQLite is canonical at runtime)
        "pytest",           # test framework
    }
    leaked = packages & dev_only
    assert not leaked, (
        f"Dev-only packages leaked into requirements-api.txt: {sorted(leaked)}. "
        "These should live in requirements.txt only. The bundle BVT will not "
        "catch this kind of bloat (the runtime works fine with extra packages); "
        "this assertion is the gate."
    )


def test_packaged_runtime_uses_headless_opencv():
    """opencv-python (full GUI) pulls Qt/X11 libs that aren't useful in a
    headless API runtime. requirements-api.txt should pin the headless variant."""
    packages = _normalized_package_names(RUNTIME_REQUIREMENTS)
    assert "opencv-python-headless" in packages, (
        "requirements-api.txt should use opencv-python-headless, not the full "
        "opencv-python — the API/indexer runtime never opens a GUI."
    )
    assert "opencv-python" not in packages, (
        "Both opencv-python and opencv-python-headless should not be listed; "
        "the bundled runtime only needs the headless variant."
    )


def test_packaged_runtime_includes_facenet_pytorch_runtime_deps():
    """facenet-pytorch is installed --no-deps in setup scripts (to prevent
    torch downgrade), so its runtime dependencies must be listed explicitly
    here. Without this, the bundle accidentally relied on transitive
    resolution from other packages — an unstable contract that broke when
    upstream packages dropped those transitive deps (PR #102 history)."""
    packages = _normalized_package_names(RUNTIME_REQUIREMENTS)
    # facenet-pytorch's runtime deps: numpy, Pillow, requests, torch, torchvision, tqdm.
    # numpy / Pillow / torch are general top-level entries; the rest are
    # explicit defensive listings.
    for pkg in ("requests", "torchvision", "tqdm"):
        assert pkg in packages, (
            f"{pkg} is a runtime dep of facenet-pytorch (installed --no-deps). "
            "Must be listed explicitly in requirements-api.txt so the contract "
            "doesn't break if upstream packages stop pulling it transitively."
        )


def test_packaged_runtime_includes_exifread_for_gps_fallback():
    """exifread is the fallback GPS parser in src/msa_indexer/io/exif.py
    (used when Pillow's GPS path returns nothing — common for HEIC/JPEG
    variants). Without it, GPS extraction silently degrades on those files."""
    packages = _normalized_package_names(RUNTIME_REQUIREMENTS)
    assert "exifread" in packages, (
        "requirements-api.txt must include exifread — src/msa_indexer/io/exif.py "
        "uses it as the fallback when Pillow's GPS path returns nothing."
    )


def test_macos_setup_applies_runtime_compatibility_overrides():
    text = MACOS_SETUP.read_text(encoding="utf-8")

    assert 'sentence-transformers' in text
    assert 'numpy<2' in text
    assert 'torch>=2.4,<2.7' in text
    assert 'facenet-pytorch>=2.6.0' in text and '--no-deps' in text


def test_macos_setup_uses_non_editable_install_for_system_app():
    text = MACOS_SETUP.read_text(encoding="utf-8")

    assert 'if msa_is_macos_system_install;' in text
    # System install: copies source to a writable tmp dir, installs from there without
    # editable mode (the app dir is root-owned and egg-info writes would fail).
    assert '"$UV_BIN" pip install --python "$VENV/bin/python" --no-deps "$TMP_SRC"' in text
    assert '"$UV_BIN" pip install --python "$VENV/bin/python" -e "$MSA_ROOT"' in text
