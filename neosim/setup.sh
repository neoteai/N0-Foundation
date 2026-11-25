#!/usr/bin/env bash
# =============================================================================
# One-shot setup for a UniVTAC test node (RTX 4090). Run from the neosim/ directory: bash setup.sh
# Canonical versions: docs/EnvironmentLock.md / requirements.lock; step-by-step guide and
# known pitfalls: docs/NODE_SETUP.md.
# Best-effort: pure-PyPI parts are fully automatic; heavy dependencies
# (isaacsim/IsaacLab/TacEx/libuipc) are self-checked after install, with clear
# manual instructions printed on failure.
# =============================================================================
set -euo pipefail

# ============================ Config (edit as needed) ========================
ENV_NAME=${ENV_NAME:-UniVTAC}
PY_VER=${PY_VER:-3.10}
WORK=${WORK:-$HOME/CodeSpace}                 # IsaacLab / curobo are cloned here
ISAACLAB_REF=${ISAACLAB_REF:-90b79bb2d44feb8d833f260f2bf37da3487180ba}   # v2.1.1
CUROBO_REF=${CUROBO_REF:-d64c4b005459db10c5dd867d8b30a87d5bda9bdb}
SKIP_APT=${SKIP_APT:-0}
# ============================================================================
REPO=$(cd "$(dirname "$0")" && pwd)
say(){ echo -e "\n\033[1;36m[setup]\033[0m $*"; }
mkdir -p "$WORK"

say "[0/9] Preflight checks (expect Ubuntu 22.04 / glibc 2.35 / driver >= 535)"
echo "  OS: $(lsb_release -ds 2>/dev/null || echo '?') | glibc $(ldd --version|head -1|grep -oE '[0-9]+\.[0-9]+$' || echo '?') | driver $(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null|head -1 || echo none)"
case "$(lsb_release -rs 2>/dev/null)" in 22.04) :;; *) echo "  WARNING: not Ubuntu 22.04; glibc mismatch may require building wheels from source (see docs/NODE_SETUP.md)";; esac

if [ "$SKIP_APT" != 1 ]; then
  say "[1/9] apt dependencies"; sudo apt-get update -qq && sudo apt-get install -y ffmpeg build-essential git cmake
fi

say "[2/9] conda env: $ENV_NAME (python $PY_VER)"
source "$(conda info --base)/etc/profile.d/conda.sh"
conda create -y -n "$ENV_NAME" python="$PY_VER" 2>/dev/null || echo "  env exists, reusing"
conda activate "$ENV_NAME"

say "[3/9] torch 2.7.0+cu128"
pip install torch==2.7.0+cu128 torchvision==0.22.0+cu128 --index-url https://download.pytorch.org/whl/cu128

say "[4/9] Isaac Sim 4.5.0.0 (pip)"
pip install 'isaacsim[all,extscache]==4.5.0.0' --extra-index-url https://pypi.nvidia.com

say "[5/9] Isaac Lab v2.1.1 (@$ISAACLAB_REF)"
[ -d "$WORK/IsaacLab/.git" ] || git clone https://github.com/isaac-sim/IsaacLab "$WORK/IsaacLab"
git -C "$WORK/IsaacLab" fetch -q origin && git -C "$WORK/IsaacLab" checkout -q "$ISAACLAB_REF"
( cd "$WORK/IsaacLab" && ./isaaclab.sh -i )    # editable-install the five source/* extensions

say "[6/9] curobo (NVlabs @$CUROBO_REF)"
[ -d "$WORK/curobo/.git" ] || git clone https://github.com/NVlabs/curobo "$WORK/curobo"
git -C "$WORK/curobo" fetch -q origin && git -C "$WORK/curobo" checkout -q "$CUROBO_REF"
pip install -e "$WORK/curobo" --no-build-isolation

say "[7/9] TacEx (in-repo third_party, builds libuipc/pyuipc)"
if [ -d "$REPO/third_party/TacEx" ]; then
  ( cd "$REPO/third_party/TacEx" && bash install.sh ) 2>/dev/null \
    || pip install -e "$REPO/third_party/TacEx/source/tacex_assets" \
                   -e "$REPO/third_party/TacEx/source/tacex" \
                   -e "$REPO/third_party/TacEx/source/tacex_uipc" \
                   -e "$REPO/third_party/TacEx/source/tacex_tasks" \
    || echo "  WARNING: TacEx/libuipc needs a manual build, see third_party/TacEx/README + docs/NODE_SETUP.md"
else
  echo "  WARNING: third_party/TacEx missing (LFS/submodules may be incomplete), see docs/NODE_SETUP.md"
fi

say "[8/9] Remaining PyPI deps + torch_scatter (custom wheel)"
pip install numpy==1.26.4 warp-lang==1.12.1 transformers==5.5.4 trimesh==4.11.5 opencv-python==4.11.0.86 scipy==1.15.3
pip install "https://data.pyg.org/whl/torch-2.8.0+cu128/torch_scatter-2.1.2+pt28cu128-cp310-cp310-linux_x86_64.whl" || echo "  torch_scatter install failed; build from source if needed (see docs)"

say "[9/9] Binary assets (USD stages, textures, calibration packs) from HuggingFace"
ASSETS_REPO=${ASSETS_REPO:-NeoteAI/NeoSim-Assets}
TACEX_DATA="$REPO/third_party/TacEx/source/tacex_assets/tacex_assets/data"
if [ -f "$REPO/assets/objects/USB.usd" ] && [ -f "$TACEX_DATA/Props/ball_wood.usd" ]; then
  echo "  already present, skipping (delete them to force a refresh)"
else
  pip install -q -U huggingface_hub
  ASSET_TMP=$(mktemp -d)
  if python -c "
from huggingface_hub import snapshot_download
snapshot_download('$ASSETS_REPO', repo_type='dataset', local_dir='$ASSET_TMP')
"; then
    mkdir -p "$REPO/assets" "$TACEX_DATA"
    cp -r "$ASSET_TMP/assets/." "$REPO/assets/"
    cp -r "$ASSET_TMP/tacex_data/." "$TACEX_DATA/"
    rm -rf "$ASSET_TMP"
    echo "  assets unpacked into assets/ and third_party/TacEx/.../data/"
  else
    echo "  WARNING: asset download failed. Set HF_TOKEN if the dataset is private,"
    echo "           or follow README > Assets to fetch them manually."
  fi
fi

say "Done. Self-check:"
python - <<'PY' || echo "  FAIL: import error, debug against docs/EnvironmentLock.md"
import torch, isaaclab, tacex_uipc
print("  torch", torch.__version__, "| cuda", torch.version.cuda)
print("  OK: torch/isaaclab/tacex_uipc all import")
PY
echo "  Final acceptance: bash collect_data.sh insert_hole demo 0 0 0 1  (one successful episode = env OK)"
echo "  Eval: see eval/run_eval.sh + docs/NODE_SETUP.md (server/client)"
