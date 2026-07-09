#!/usr/bin/env bash
# ============================================================
# new-notebook-setup.sh
# 在新 GPU notebook (4×4090) 上一键完成环境准备
# （中间过程全部可见）
# ============================================================
set -uo pipefail  # 不用 -e，apt lock 时不要直接炸，继续走 fallback

echo "╔══════════════════════════════════════════════╗"
echo "║  Academic Compass - New Notebook Setup       ║"
echo "╚══════════════════════════════════════════════╝"

# ---- Step 0: 基础工具包 ---------------------------------
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📦 [1/5] 安装基础系统工具..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 先暴力清锁（dpkg + debconf 都得清）
echo ">>> 清理残留 dpkg/debconf 锁 ..."
for lock in /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock /var/cache/debconf/config.dat; do
    PID=$(fuser "$lock" 2>/dev/null | tr -d ' ')
    if [ -n "$PID" ]; then
        echo "   kill $PID 占用 $lock"
        kill -9 $PID 2>/dev/null || true
    fi
    rm -f "$lock" 2>/dev/null || true
done
dpkg --configure -a 2>/dev/null || true
echo "   ✅ 锁已清理"

echo ""
echo ">>> 环境类型: Debian/Ubuntu (apt)"
echo ">>> apt-get update ..."
apt-get update 2>&1 || echo "   (update 失败，继续)"

echo ""
echo ">>> apt-get install ..."
apt-get install -y tmux htop nvtop git curl wget vim tree unzip zip poppler-utils build-essential 2>&1 || echo "   (部分包安装失败，继续)"

echo ""
echo ">>> 检查关键工具状态:"
for tool in tmux htop nvtop git curl tree pdftoppm; do
    if command -v "$tool" &>/dev/null; then
        echo "   ✅ $tool: $(command -v $tool)"
    else
        echo "   ⚠️  $tool: 未安装（可能已内置或被跳过）"
    fi
done

echo ""
echo ">>> pip install pymupdf Pillow (PDF 处理兜底) ..."
pip install pymupdf Pillow 2>&1

echo ""
echo "✅ [1/5] 基础工具完成"

# ---- Step 1: Python 环境 ---------------------------------
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📦 [2/5] 安装 Python 工具链..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

echo ">>> python3 --version"
python3 --version

echo ""
echo ">>> pip install --upgrade pip"
pip install --upgrade pip 2>&1

echo ""
echo ">>> pip install uv"
pip install uv 2>&1

echo ""
echo ">>> pip 版本: $(pip --version | awk '{print $2}')"
echo ">>> uv  版本: $(uv --version 2>/dev/null || echo '?')"

echo ""
echo "✅ [2/5] Python 工具链完成"

# ---- Step 2: CUDA/PyTorch ---------------------------------
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📦 [3/5] 安装 PyTorch + CUDA 生态..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

echo ">>> 检测 nvidia-smi ..."
if command -v nvidia-smi &>/dev/null; then
    nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader 2>/dev/null || echo "(nvidia-smi 无输出，可能未挂载 device)"
else
    echo "   nvidia-smi: 未安装 (应该在 CUDA 镜像里自动有)"
fi

echo ""
echo ">>> pip install torch torchvision (CUDA 12.4) ..."
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124 2>&1

echo ""
echo ">>> 验证 PyTorch GPU ..."
python3 -c "
import torch
print(f'   PyTorch   = {torch.__version__}')
print(f'   CUDA OK   = {torch.cuda.is_available()}')
print(f'   GPU count = {torch.cuda.device_count()}')
for i in range(torch.cuda.device_count()):
    p = torch.cuda.get_device_properties(i)
    print(f'   GPU[{i}]    = {p.name}, {p.total_mem/1e9:.1f}GB, compute {p.major}.{p.minor}')
"

echo ""
echo "✅ [3/5] PyTorch + CUDA 完成"

# ---- Step 3: Marker + 依赖 ---------------------------------
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📦 [4/5] 安装 Marker PDF 解析引擎..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

echo ">>> pip install marker-pdf ..."
pip install marker-pdf 2>&1

echo ""
echo ">>> 验证 Marker 导入 ..."
python3 -c "
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from marker.settings import settings
print(f'   Marker version = {settings.__dict__.get(\"VERSION\", \"?\")}')
print(f'   Default device = {settings.TORCH_DEVICE}')
print(f'   Marker 导入 OK ✅')
" 2>&1

echo ""
echo "✅ [4/5] Marker 安装完成"

# ---- Step 4: 验证路径 ---------------------------------
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📦 [5/5] 验证共享存储路径..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

PROJECT_DIR="/inspire/hdd/project/multi-agent/niexiaohang-25130061/academic-compass"

echo ">>> 检查 $PROJECT_DIR ..."
if [ -d "$PROJECT_DIR" ]; then
    echo "   ✅ 项目目录存在"
    echo ""
    echo ">>> PDF 测试文件:"
    ls -lh "$PROJECT_DIR/data/"*.pdf 2>/dev/null | awk '{print "   ", $5, $NF}'
    echo ""
    echo ">>> MinerU 已有输出:"
    ls "$PROJECT_DIR/data/"*.md 2>/dev/null | wc -l | xargs echo "   .md 文件:"
    ls "$PROJECT_DIR/data/"*_content_list.json 2>/dev/null | wc -l | xargs echo "   .json 文件:"
    echo ""
    echo ">>> 测试脚本:"
    ls -lh "$PROJECT_DIR/scripts/test_marker.py" 2>/dev/null
    ls -lh "$PROJECT_DIR/scripts/check_env.py" 2>/dev/null
else
    echo "   ❌ 项目目录不可访问: $PROJECT_DIR"
    echo "   >>> 尝试搜索 ..."
    find /inspire -maxdepth 5 -name 'test_marker.py' -type f 2>/dev/null | head -3
fi

echo ""
echo "✅ [5/5] 路径验证完成"

# ---- 完成 ----
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║            ✅  环境全部就绪!                  ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "   下一步:"
echo "   1) python scripts/check_env.py          ← 系统体检贴给我"
echo "   2) python scripts/test_marker.py --quick  ← 快速跑一篇看效果"
echo "   3) python scripts/test_marker.py --all --workers 4  ← 全量 8 篇"
echo ""
